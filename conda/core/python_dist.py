# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import namedtuple
from csv import reader as csv_reader
from email.parser import HeaderParser
from fnmatch import filter as fnmatch_filter
from os import listdir
from os.path import basename, dirname, isdir, isfile, join, lexists, normpath
import re
import warnings

from .python_markers import interpret
from .._vendor.auxlib.decorators import memoizedproperty
from .._vendor.frozendict import frozendict
from ..common.compat import PY2, StringIO, itervalues, odict, open
from ..common.path import (get_major_minor_version, get_python_site_packages_short_path, pyc_path,
                           win_path_ok)
from ..models.channel import Channel
from ..models.enums import PackageType, PathType
from ..models.records import PathData, PathDataV1, PathsData, PrefixRecord

try:
    from ConfigParser import ConfigParser
except ImportError:
    from configparser import ConfigParser

try:
    from cytoolz.itertoolz import concat, concatv, groupby
except ImportError:  # pragma: no cover
    from .._vendor.toolz.itertoolz import concat, concatv, groupby  # NOQA


# TODO: complete this list
PYPI_TO_CONDA = {
    'graphviz': 'python-graphviz',
}
# TODO: complete this list
PYPI_CONDA_DEPS = {
    'graphviz': ['graphviz'],  # What version constraints?
}
# This regex can process requirement including or not including name.
# This is useful for parsing, for example, `Python-Version`
PARTIAL_PYPI_SPEC_PATTERN = re.compile(r'''
    # Text needs to be stripped and all extra spaces replaced by single spaces
    (?P<name>^[A-Z0-9][A-Z0-9._-]*)?
    \s?
    (\[(?P<extras>.*)\])?
    \s?
    (?P<constraints>\(? \s? ([\w\d<>=!~,\s\.\*]*) \s? \)? )?
    \s?
''', re.VERBOSE | re.IGNORECASE)
PySpec = namedtuple('PySpec', ['name', 'extras', 'constraints', 'marker', 'url'])


# Main functions
# -----------------------------------------------------------------------------
def get_python_records(anchor_files, prefix_path, python_version):
    """
    Process all anchor files and return a python record.

    This method evaluates the context needed for marker evaluation.
    """
    python_version = get_major_minor_version(python_version)
    return tuple(
        pyrec for pyrec in (
            get_python_record(anchor_file, prefix_path, python_version)
            for anchor_file in sorted(anchor_files)
        ) if pyrec
    )


def get_python_record(anchor_file, prefix_path, python_version):
    """
    Convert a python package defined by an anchor file (Metadata information)
    into a conda prefix record object.

    Return `None` if the python record cannot be created.
    """
    # TODO: ensure that this dist is actually the dist that matches conda-meta
    pydist = get_python_distribution_info(prefix_path, anchor_file, python_version)
    return None if pydist is None else pydist.prefix_record


# Python distribution/eggs metadata
# -----------------------------------------------------------------------------
class MetadataWarning(Warning):
    pass


class PythonDistributionMetadata(object):
    """
    Object representing the metada of a Python Distribution given by anchor
    file (or directory) path.

    This metadata is extracted from a single file. Python distributions might
    create additional files that complement this metadata information, but
    that is handled at the python distribution level.

    Notes
    -----
      - https://packaging.python.org/specifications/core-metadata/
      - Metadata 2.1: https://www.python.org/dev/peps/pep-0566/
      - Metadata 2.0: https://www.python.org/dev/peps/pep-0426/ (Withdrawn)
      - Metadata 1.2: https://www.python.org/dev/peps/pep-0345/
      - Metadata 1.1: https://www.python.org/dev/peps/pep-0314/
      - Metadata 1.0: https://www.python.org/dev/peps/pep-0241/
    """
    FILE_NAMES = ('METADATA', 'PKG-INFO')

    # Python Packages Metadata 2.1
    # -----------------------------------------------------------------------------
    SINGLE_USE_KEYS = frozendict((
        ('Metadata-Version', 'metadata_version'),
        ('Name', 'name'),
        ('Version', 'version'),
        # ('Summary', 'summary'),
        # ('Description', 'description'),
        # ('Description-Content-Type', 'description_content_type'),
        # ('Keywords', 'keywords'),
        # ('Home-page', 'home_page'),
        # ('Download-URL', 'download_url'),
        # ('Author', 'author'),
        # ('Author-email', 'author_email'),
        # ('Maintainer', 'maintainer'),
        # ('Maintainer-email', 'maintainer_email'),
        ('License', 'license'),
        # # Deprecated
        # ('Obsoleted-By', 'obsoleted_by'),  # Note: See 2.0
        # ('Private-Version', 'private_version'),  # Note: See 2.0
    ))
    MULTIPLE_USE_KEYS = frozendict((
        ('Platform', 'platform'),
        ('Supported-Platform', 'supported_platform'),
        # ('Classifier', 'classifier'),
        ('Requires-Dist', 'requires_dist'),
        ('Requires-External', 'requires_external'),
        ('Requires-Python', 'requires_python'),
        # ('Project-URL', 'project_url'),
        ('Provides-Extra', 'provides_extra'),
        # ('Provides-Dist', 'provides_dist'),
        # ('Obsoletes-Dist', 'obsoletes_dist'),
        # # Deprecated
        # ('Extension', 'extension'),  # Note: See 2.0
        # ('Obsoletes', 'obsoletes'),
        # ('Provides', 'provides'),
        ('Requires', 'requires'),
        # ('Setup-Requires-Dist', 'setup_requires_dist'),  # Note: See 2.0
    ))

    def __init__(self, path):
        metadata_path = self._process_path(path, self.FILE_NAMES)
        self._path = path
        self._data = self._read_metadata(metadata_path)

    @staticmethod
    def _process_path(path, metadata_filenames):
        """Find metadata file inside dist-info folder, or check direct file."""
        metadata_path = None
        if path:
            if isdir(path):
                for fname in metadata_filenames:
                    fpath = join(path, fname)
                    if isfile(fpath):
                        metadata_path = fpath
                        break
            elif isfile(path):
                # '<pkg>.egg-info' file contains metadata directly
                filenames = ['.egg-info']
                if metadata_filenames:
                    filenames.extend(metadata_filenames)
                assert any(path.endswith(filename) for filename in filenames)
                metadata_path = path
            else:
                # `path` does not exist
                warnings.warn("Metadata path not found", MetadataWarning)
        else:
            warnings.warn("Metadata path not found", MetadataWarning)

        return metadata_path

    @classmethod
    def _message_to_dict(cls, message):
        """
        Convert the RFC-822 headers data into a dictionary.

        `message` is an email.parser.Message instance.

        The canonical method to transform metadata fields into such a data
        structure is as follows:
          - The original key-value format should be read with
            email.parser.HeaderParser
          - All transformed keys should be reduced to lower case. Hyphens
            should be replaced with underscores, but otherwise should retain
            all other characters
          - The transformed value for any field marked with "(Multiple-use")
            should be a single list containing all the original values for the
            given key
          - The Keywords field should be converted to a list by splitting the
            original value on whitespace characters
          - The message body, if present, should be set to the value of the
            description key.
          - The result should be stored as a string-keyed dictionary.
        """
        new_data = odict()

        if message:
            for key, value in message.items():

                if key in cls.MULTIPLE_USE_KEYS:
                    new_key = cls.MULTIPLE_USE_KEYS[key]
                    if new_key not in new_data:
                        new_data[new_key] = [value]
                    else:
                        new_data[new_key].append(value)

                elif key in cls.SINGLE_USE_KEYS:
                    new_key = cls.SINGLE_USE_KEYS[key]
                    new_data[new_key] = value

            # TODO: Handle license later on for convenience

        return new_data

    @classmethod
    def _read_metadata(cls, fpath):
        """
        Read the original format which is stored as RFC-822 headers.
        """
        data = odict()
        if fpath and isfile(fpath):
            parser = HeaderParser()

            # FIXME: Is this a correct assumption for the encoding?
            # This was needed due to some errors on windows
            with open(fpath) as fp:
                data = parser.parse(fp)

        return cls._message_to_dict(data)

    def _get_multiple_data(self, keys):
        """
        Helper method to get multiple data values by keys.

        Keys is an iterable including the prefered key in order, to include
        values of key that might have been replaced (deprecated), for example
        keys can be ['requires_dist', 'requires'], where the key 'requires' is
        deprecated and replaced by 'requires_dist'.
        """
        data = []
        if self._data:
            for key in keys:
                raw_data = self._data.get(key, [])
                for req in raw_data:
                    data.append(req.strip())

                if data:
                    break

        return frozenset(data)

    def get_dist_requirements(self):
        """
        Changed in version 2.1: The field format specification was relaxed to
        accept the syntax used by popular publishing tools.

        Each entry contains a string naming some other distutils project
        required by this distribution.

        The format of a requirement string contains from one to four parts:
          - A project name, in the same format as the Name: field. The only
            mandatory part.
          - A comma-separated list of ‘extra’ names. These are defined by the
            required project, referring to specific features which may need
            extra dependencies.
          - A version specifier. Tools parsing the format should accept
            optional parentheses around this, but tools generating it should
            not use parentheses.
          - An environment marker after a semicolon. This means that the
            requirement is only needed in the specified conditions.

        This field may be followed by an environment marker after a semicolon.

        Example
        -------
        frozenset(['pkginfo', 'PasteDeploy', 'zope.interface (>3.5.0)',
                   'pywin32 >1.0; sys_platform == "win32"'])

        Return 'Requires' if 'Requires-Dist' is empty.
        """
        return self._get_multiple_data(['requires_dist', 'requires'])

    def get_python_requirements(self):
        """
        New in version 1.2.

        This field specifies the Python version(s) that the distribution is
        guaranteed to be compatible with. Installation tools may look at this
        when picking which version of a project to install.

        The value must be in the format specified in Version specifiers.

        This field may be followed by an environment marker after a semicolon.

        Example
        -------
        frozenset(['>=3', '>2.6,!=3.0.*,!=3.1.*', '~=2.6',
                   '>=3; sys_platform == "win32"'])
        """
        return self._get_multiple_data(['requires_python'])

    def get_external_requirements(self):
        """
        Changed in version 2.1: The field format specification was relaxed to
        accept the syntax used by popular publishing tools.

        Each entry contains a string describing some dependency in the system
        that the distribution is to be used. This field is intended to serve
        as a hint to downstream project maintainers, and has no semantics
        which are meaningful to the distutils distribution.

        The format of a requirement string is a name of an external dependency,
        optionally followed by a version declaration within parentheses.

        This field may be followed by an environment marker after a semicolon.

        Because they refer to non-Python software releases, version numbers for
        this field are not required to conform to the format specified in PEP
        440: they should correspond to the version scheme used by the external
        dependency.

        Notice that there’s is no particular rule on the strings to be used!

        Example
        -------
        frozenset(['C', 'libpng (>=1.5)', 'make; sys_platform != "win32"'])
        """
        return self._get_multiple_data(['requires_external'])

    def get_extra_provides(self):
        """
        New in version 2.1.

        A string containing the name of an optional feature. Must be a valid
        Python identifier. May be used to make a dependency conditional on
        hether the optional feature has been requested.

        Example
        -------
        frozenset(['pdf', 'doc', 'test'])
        """
        return self._get_multiple_data(['provides_extra'])

    def get_dist_provides(self):
        """
        New in version 1.2.

        Changed in version 2.1: The field format specification was relaxed to
        accept the syntax used by popular publishing tools.

        Each entry contains a string naming a Distutils project which is
        contained within this distribution. This field must include the project
        identified in the Name field, followed by the version : Name (Version).

        A distribution may provide additional names, e.g. to indicate that
        multiple projects have been bundled together. For instance, source
        distributions of the ZODB project have historically included the
        transaction project, which is now available as a separate distribution.
        Installing such a source distribution satisfies requirements for both
        ZODB and transaction.

        A distribution may also provide a “virtual” project name, which does
        not correspond to any separately-distributed project: such a name might
        be used to indicate an abstract capability which could be supplied by
        one of multiple projects. E.g., multiple projects might supply RDBMS
        bindings for use by a given ORM: each project might declare that it
        provides ORM-bindings, allowing other projects to depend only on having
        at most one of them installed.

        A version declaration may be supplied and must follow the rules
        described in Version specifiers. The distribution’s version number
        will be implied if none is specified.

        This field may be followed by an environment marker after a semicolon.

        Return `Provides` in case `Provides-Dist` is empty.
        """
        return self._get_multiple_data(['provides_dist', 'provides'])

    def get_dist_obsolete(self):
        """
        New in version 1.2.

        Changed in version 2.1: The field format specification was relaxed to
        accept the syntax used by popular publishing tools.

        Each entry contains a string describing a distutils project’s
        distribution which this distribution renders obsolete, meaning that
        the two projects should not be installed at the same time.

        Version declarations can be supplied. Version numbers must be in the
        format specified in Version specifiers [1].

        The most common use of this field will be in case a project name
        changes, e.g. Gorgon 2.3 gets subsumed into Torqued Python 1.0. When
        you install Torqued Python, the Gorgon distribution should be removed.

        This field may be followed by an environment marker after a semicolon.

        Return `Obsoletes` in case `Obsoletes-Dist` is empty.

        Example
        -------
        frozenset(['Gorgon', "OtherProject (<3.0) ; python_version == '2.7'"])

        Notes
        -----
        - [1] https://packaging.python.org/specifications/version-specifiers/
        """

        return self._get_multiple_data(['obsoletes_dist', 'obsoletes'])

    def get_classifiers(self):
        """
        Classifiers are described in PEP 301, and the Python Package Index
        publishes a dynamic list of currently defined classifiers.

        This field may be followed by an environment marker after a semicolon.

        Example
        -------
        frozenset(['Development Status :: 4 - Beta',
                   "Environment :: Console (Text Based) ; os_name == "posix"])
        """
        return self._get_multiple_data(['classifier'])

    @property
    def name(self):
        return self._data.get('name')  # TODO: Check for existence?

    @property
    def version(self):
        return self._data.get('version')  # TODO: Check for existence?


# Dist classes
# -----------------------------------------------------------------------------
class BasePythonDistribution(object):
    """
    Base object describing a python distribution based on path to anchor file.
    """
    MANIFEST_FILES = ()   # Only one is used, but many names available
    REQUIRES_FILES = ()  # Only one is used, but many names available
    MANDATORY_FILES = ()
    ENTRY_POINTS_FILES = ('entry_points.txt', )

    channel = Channel("pypi")
    build = "pypi_0"

    def __init__(self, anchor_full_path, python_version):
        self.anchor_full_path = anchor_full_path
        self.python_version = python_version

        if anchor_full_path and isfile(anchor_full_path):
            self._metadata_dir_full_path = dirname(anchor_full_path)
        elif anchor_full_path and isdir(anchor_full_path):
            self._metadata_dir_full_path = anchor_full_path
        else:
            self._metadata_dir_full_path = None
            # raise RuntimeError("Path not found: %s", anchor_full_path)

        self._check_files()
        self._metadata = PythonDistributionMetadata(anchor_full_path)
        self._provides_file_data = ()
        self._requires_file_data = ()

    def _check_files(self):
        """Check the existence of mandatory files for a given distribution."""
        for fname in self.MANDATORY_FILES:
            if self._metadata_dir_full_path:
                fpath = join(self._metadata_dir_full_path, fname)
                assert isfile(fpath)

    def _check_path_data(self, path, checksum, size):
        """Normalizes record data content and format."""
        if checksum:
            assert checksum.startswith('sha256='), (self._metadata_dir_full_path, path, checksum)
            checksum = checksum[7:]
        else:
            checksum = None
        size = int(size) if size else None

        return path, checksum, size

    @staticmethod
    def _parse_requires_file_data(data, global_section='__global__'):
        """
        https://setuptools.readthedocs.io/en/latest/formats.html#requires-txt
        """
        requires = odict()
        lines = [l.strip() for l in data.split('\n') if l]

        if lines and not (lines[0].startswith('[') and lines[0].endswith(']')):
            # Add dummy section for unsectioned items
            lines = ['[{}]'.format(global_section)] + lines

        # Parse sections
        for line in lines:
            if line.startswith('[') and line.endswith(']'):
                section = line.strip()[1:-1]
                requires[section] = []
                continue

            if line.strip():
                requires[section].append(line.strip())

        # Adapt to *standard* requirements (add env markers to requirements)
        reqs = []
        extras = []
        for section, values in requires.items():
            if section == global_section:
                # This is the global section (same as dist_requires)
                reqs.extend(values)
            elif section.startswith(':'):
                # The section is used as a marker
                # Example: ":python_version < '3'"
                marker = section.replace(':', '; ')
                new_values = [v+marker for v in values]
                reqs.extend(new_values)
            else:
                # The section is an extra, i.e. "docs", or "tests"...
                extras.append(section)
                marker = '; extra == "{}"'.format(section)
                new_values = [v+marker for v in values]
                reqs.extend(new_values)

        return frozenset(reqs), extras

    @staticmethod
    def _parse_entries_file_data(data):
        """
        https://setuptools.readthedocs.io/en/latest/formats.html#entry-points-txt-entry-point-plugin-metadata
        """
        # FIXME: Use pkg_resources which provides API for this?
        entries_data = odict()
        config = ConfigParser()
        config.optionxform = lambda x: x  # Avoid lowercasing keys
        config.readfp(StringIO(data))
        for section in config.sections():
            entries_data[section] = odict(config.items(section))

        return entries_data

    def _load_requires_provides_file(self):
        """
        https://setuptools.readthedocs.io/en/latest/formats.html#requires-txt
        """
        # FIXME: Use pkg_resources which provides API for this?
        requires, extras = None, None
        for fname in self.REQUIRES_FILES:
            fpath = join(self._metadata_dir_full_path, fname)
            if isfile(fpath):
                with open(fpath, 'r') as fh:
                    data = fh.read()

                requires, extras = self._parse_requires_file_data(data)
                self._provides_file_data = extras
                self._requires_file_data = requires
                break

        return requires, extras

    @memoizedproperty
    def manifest_full_path(self):
        manifest_full_path = None
        if self._metadata_dir_full_path:
            for fname in self.MANIFEST_FILES:
                manifest_full_path = join(self._metadata_dir_full_path, fname)
                if isfile(manifest_full_path):
                    break
        return manifest_full_path

    def _get_paths(self):
        """
        Read the list of installed paths from record or source file.

        Example
        -------
        [(u'skdata/__init__.py', u'sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU', 0),
         (u'skdata/diabetes.py', None, None),
         ...
        ]
        """
        manifest_full_path = self.manifest_full_path
        if manifest_full_path:
            python_version = self.python_version
            sp_dir = get_python_site_packages_short_path(python_version) + "/"
            prepend_metadata_dirname = basename(manifest_full_path) == "installed-files.txt"
            if prepend_metadata_dirname:
                path_prepender = basename(dirname(manifest_full_path)) + "/"
            else:
                path_prepender = ""

            def process_csv_row(row):
                cleaned_path = normpath("%s%s%s" % (sp_dir, path_prepender, row[0]))
                if len(row) == 3:
                    checksum, size = row[1:]
                    if checksum:
                        assert checksum.startswith('sha256='), (self._metadata_dir_full_path,
                                                                cleaned_path, checksum)
                        checksum = checksum[7:]
                    else:
                        checksum = None
                    size = int(size) if size else None
                else:
                    checksum = size = None
                return cleaned_path, checksum, size

            csv_delimiter = ','
            if PY2:
                csv_delimiter = csv_delimiter.encode('utf-8')
            with open(manifest_full_path) as csvfile:
                record_reader = csv_reader(csvfile, delimiter=csv_delimiter)
                # format of each record is (path, checksum, size)
                records = [process_csv_row(row) for row in record_reader if row[0]]
            files = set(record[0] for record in records)

            _pyc_path = pyc_path
            py_file_re = re.compile(r'^[^\t\n\r\f\v]+/site-packages/[^\t\n\r\f\v]+\.py$')

            missing_pyc_files = (ff for ff in (
                _pyc_path(f, python_version) for f in files if py_file_re.match(f)
            ) if ff not in files)
            records = sorted(records + [(pf, None, None) for pf in missing_pyc_files])
            return records

        return []

    def get_dist_requirements(self):
        # FIXME: On some packages, requirements are not added to metadata,
        # but on a separate requires.txt, see: python setup.py develop for
        # anaconda-client. This is setuptools behavior.
        # TODO: what is the dependency_links.txt on the same example?
        data = self._metadata.get_dist_requirements()
        if self._requires_file_data:
            data = self._requires_file_data
        elif not data:
            self._load_requires_provides_file()
            data = self._requires_file_data
        return data

    def get_python_requirements(self):
        return self._metadata.get_python_requirements()

    def get_external_requirements(self):
        return self._metadata.get_external_requirements()

    def get_extra_provides(self):
        # FIXME: On some packages, requirements are not added to metadata,
        # but on a separate requires.txt, see: python setup.py develop for
        # anaconda-client. This is setuptools behavior.
        data = self._metadata.get_extra_provides()
        if self._provides_file_data:
            data = self._provides_file_data
        elif not data:
            self._load_requires_provides_file()
            data = self._provides_file_data

        return data

    def get_conda_dependencies(self):
        """
        Process metadata fields providing dependency information.

        This includes normalizing fields, and evaluating environment markers.
        """
        python_spec = "python %s.*" % ".".join(self.python_version.split('.')[:2])

        def pyspec_to_norm_req(pyspec):
            conda_name = pypi_name_to_conda_name(norm_package_name(pyspec.name))
            return "%s %s" % (conda_name, pyspec.constraints) if pyspec.constraints else conda_name

        reqs = self.get_dist_requirements()
        pyspecs = tuple(parse_specification(req) for req in reqs)
        marker_groups = groupby(lambda ps: ps.marker.split("==", 1)[0].strip(), pyspecs)
        depends = set(pyspec_to_norm_req(pyspec) for pyspec in marker_groups.pop("", ()))
        extras = marker_groups.pop("extra", ())
        execution_context = {
            "python_version": self.python_version,
        }
        depends.update(
            pyspec_to_norm_req(pyspec) for pyspec in concat(itervalues(marker_groups))
            if interpret(pyspec.marker, execution_context)
        )
        constrains = set(pyspec_to_norm_req(pyspec) for pyspec in extras if pyspec.constraints)
        depends.add(python_spec)

        return sorted(depends), sorted(constrains)

    def get_optional_dependencies(self):
        raise NotImplementedError

    def get_entry_points(self):
        # TODO: need to add entry points, "exports," and other files that might
        # not be in RECORD
        for fname in self.ENTRY_POINTS_FILES:
            fpath = join(self._metadata_dir_full_path, fname)
            if isfile(fpath):
                with open(fpath, 'r') as fh:
                    data = fh.read()
        return self._parse_entries_file_data(data)

    def get_paths_data(self):
        raise NotImplementedError

    @property
    def name(self):
        return self._metadata.name

    @property
    def norm_name(self):
        return norm_package_name(self.name)

    @property
    def conda_name(self):
        return pypi_name_to_conda_name(self.norm_name)

    @property
    def version(self):
        return self._metadata.version

    @property
    def prefix_record(self):
        paths_data, files = self.get_paths_data()
        depends, constrains = self.get_conda_dependencies()
        return PrefixRecord(
            package_type=self.package_type,
            name=self.conda_name,
            version=self.version,
            channel=self.channel,
            subdir="pypi",
            fn=self.sp_reference,
            build=self.build,
            build_number=0,
            paths_data=paths_data,
            files=files,
            depends=depends,
            constrains=constrains,
        )


class PythonInstalledDistribution(BasePythonDistribution):
    """
    Python distribution installed via distutils.

    Notes
    -----
      - https://www.python.org/dev/peps/pep-0376/
    """
    MANIFEST_FILES = ('RECORD',)
    REQUIRES_FILES = ()
    MANDATORY_FILES = ('METADATA', )
    # FIXME: Do this check? Disabled for tests where only Metadata file is stored
    # MANDATORY_FILES = ('METADATA', 'RECORD', 'INSTALLER')
    ENTRY_POINTS_FILES = ()

    package_type = PackageType.VIRTUAL_PYTHON_WHEEL

    def __init__(self, prefix_path, anchor_file, python_version):
        anchor_full_path = join(prefix_path, win_path_ok(dirname(anchor_file)))
        super(PythonInstalledDistribution, self).__init__(anchor_full_path, python_version)
        self.sp_reference = basename(dirname(anchor_file))

    def get_paths_data(self):
        paths_data = [PathDataV1(
            _path=path, path_type=PathType.hardlink, sha256=checksum, size_in_bytes=size
        ) for (path, checksum, size) in self._get_paths()]
        files = [pd._path for pd in paths_data]
        return PathsData(paths_version=1, paths=paths_data), files


class PythonEggInfoDistribution(BasePythonDistribution):
    """
    Python distribution installed via setuptools.

    Notes
    -----
      - http://peak.telecommunity.com/DevCenter/EggFormats
    """
    MANIFEST_FILES = ('installed-files.txt', 'SOURCES', 'SOURCES.txt')
    REQUIRES_FILES = ('requires.txt', 'depends.txt')
    MANDATORY_FILES = ()
    ENTRY_POINTS_FILES = ('entry_points.txt', )

    def __init__(self, anchor_full_path, python_version, sp_reference):
        super(PythonEggInfoDistribution, self).__init__(anchor_full_path, python_version)
        self.sp_reference = sp_reference

    def get_paths_data(self):
        if self.package_type == PackageType.VIRTUAL_PYTHON_EGG_MANAGEABLE:
            files = [path for path, _, _ in self._get_paths()]
            paths_data = [PathData(_path=path, path_type=PathType.hardlink) for path in files]
            return PathsData(paths_version=1, paths=paths_data), files
        else:
            return PathsData(paths_version=1, paths=()), ()

    @property
    def package_type(self):
        if self.manifest_full_path and basename(self.manifest_full_path) == "installed-files.txt":
            return PackageType.VIRTUAL_PYTHON_EGG_MANAGEABLE
        else:
            return PackageType.VIRTUAL_PYTHON_EGG_UNMANAGEABLE


class PythonEggLinkDistribution(PythonEggInfoDistribution):

    package_type = PackageType.VIRTUAL_PYTHON_EGG_LINK

    def __init__(self, prefix_path, anchor_file, python_version):
        anchor_full_path = get_dist_file_from_egg_link(anchor_file, prefix_path)
        sp_reference = None  # This can be None in case the egg-info is no longer there
        super(PythonEggLinkDistribution, self).__init__(anchor_full_path, python_version, sp_reference)
        self.channel = Channel("<develop>")
        self.build = "dev_0"


# Helper functions
# -----------------------------------------------------------------------------
def norm_package_name(name):
    return name.replace('.', '-').replace('_', '-').lower() if name else ''


def pypi_name_to_conda_name(pypi_name):
    return PYPI_TO_CONDA.get(pypi_name, pypi_name) if pypi_name else ''


def norm_package_version(version):
    """Normalize a version by removing extra spaces and parentheses."""
    if version:
        version = ','.join(v.strip() for v in version.split(',')).strip()

        if version.startswith('(') and version.endswith(')'):
            version = version[1:-1]

        version = ''.join(v for v in version if v.strip())
    else:
        version = ''

    return version


def split_spec(spec, sep):
    """Split a spec by separator and return stripped start and end parts."""
    parts = spec.rsplit(sep, 1)
    spec_start = parts[0].strip()
    spec_end = ''
    if len(parts) == 2:
        spec_end = parts[-1].strip()
    return spec_start, spec_end


def parse_specification(spec):
    """
    Parse a requirement from a python distribution metadata and return a
    namedtuple with name, extras, constraints, marker and url components.

    This method does not enforce strict specifications but extracts the
    information which is assumed to be *correct*. As such no errors are raised.

    Example
    -------
    PySpec(name='requests', extras=['security'], constraints='>=3.3.0',
           marker='foo >= 2.7 or bar == 1', url=''])
    """
    name, extras, const = spec, [], ''

    # Remove excess whitespace
    spec = ' '.join(p for p in spec.split(' ') if p).strip()

    # Extract marker (Assumes that there can only be one ';' inside the spec)
    spec, marker = split_spec(spec, ';')

    # Extract url (Assumes that there can only be one '@' inside the spec)
    spec, url = split_spec(spec, '@')

    # Find name, extras and constraints
    r = PARTIAL_PYPI_SPEC_PATTERN.match(spec)
    if r:
        # Normalize name
        name = r.group('name')
        name = norm_package_name(name)  # TODO: Do we want this or not?

        # Clean extras
        extras = r.group('extras')
        extras = [e.strip() for e in extras.split(',') if e] if extras else []

        # Clean constraints
        const = r.group('constraints')
        const = ''.join(c for c in const.split(' ') if c).strip()
        if const.startswith('(') and const.endswith(')'):
            # Remove parens
            const = const[1:-1]

    return PySpec(name=name, extras=extras, constraints=const, marker=marker, url=url)


def get_conda_anchor_files_and_records(python_records):
    """Return the anchor files for the conda records of python packages."""
    anchor_file_endings = ('.egg-info/PKG-INFO', '.dist-info/RECORD', '.egg-info')
    conda_python_packages = odict()

    for prefix_record in python_records:
        for fpath in prefix_record.files:
            if fpath.endswith(anchor_file_endings) and 'site-packages' in fpath:
                # Then 'fpath' is an anchor file
                conda_python_packages[fpath] = prefix_record

    return conda_python_packages


def get_site_packages_anchor_files(site_packages_path, site_packages_dir):
    """Get all the anchor files for the site packages directory."""
    site_packages_anchor_files = set()
    for fname in listdir(site_packages_path):
        anchor_file = None
        if fname.endswith('.dist-info'):
            anchor_file = "%s/%s/%s" % (site_packages_dir, fname, 'RECORD')
        elif fname.endswith(".egg-info"):
            if isfile(join(site_packages_path, fname)):
                anchor_file = "%s/%s" % (site_packages_dir, fname)
            else:
                anchor_file = "%s/%s/%s" % (site_packages_dir, fname, "PKG-INFO")
        elif fname.endswith(".egg"):
            if isdir(join(site_packages_path, fname)):
                anchor_file = "%s/%s/%s/%s" % (site_packages_dir, fname, "EGG-INFO", "PKG-INFO")
            # FIXME: If it is a .egg file, we need to unzip the content to be
            # able. Do this once and leave the directory, and remove the egg
            # (which is a zip file in disguise?)
        elif fname.endswith('.egg-link'):
            anchor_file = "%s/%s" % (site_packages_dir, fname)
        elif fname.endswith('.pth'):
            continue
        else:
            continue

        if anchor_file:
            site_packages_anchor_files.add(anchor_file)

    return site_packages_anchor_files


def get_dist_file_from_egg_link(egg_link_file, prefix_path):
    """
    Return the egg info file path following an egg link.

    Return `None` if no egg-info is found or the path is no longer there.
    """
    egg_info_full_path = None

    with open(join(prefix_path, win_path_ok(egg_link_file))) as fh:
        # See: https://setuptools.readthedocs.io/en/latest/formats.html#egg-links
        # "...Each egg-link file should contain a single file or directory name
        # with no newlines..."
        egg_link_contents = fh.readlines()[0].strip()

    if lexists(egg_link_contents):
        egg_info_fnames = fnmatch_filter(listdir(egg_link_contents), '*.egg-info')
    else:
        egg_info_fnames = ()

    if egg_info_fnames:
        assert len(egg_info_fnames) == 1, (egg_link_file, egg_info_fnames)
        egg_info_full_path = join(egg_link_contents, egg_info_fnames[0])

        if isdir(egg_info_full_path):
            egg_info_full_path = join(egg_info_full_path, "PKG-INFO")

    return egg_info_full_path


def get_python_distribution_info(prefix_path, anchor_file, python_version):
    """
    For a given anchor file return the python distribution.

    Return `None` if the information was not found (can happen with egg-links).
    """
    if anchor_file.endswith('.egg-link'):
        return PythonEggLinkDistribution(prefix_path, anchor_file, python_version)
        # sp_reference = None
        # # This can be None in case the egg-info is no longer there
        # dist_file = get_dist_file_from_egg_link(anchor_file, prefix_path)
        # dist_cls = PythonEggInfoDistribution
        # package_type = PackageType.VIRTUAL_PYTHON_EGG_LINK
    elif ".dist-info" in anchor_file:
        return PythonInstalledDistribution(prefix_path, anchor_file, python_version)
        # sp_reference = basename(dirname(anchor_file))
        # dist_file = join(prefix_path, win_path_ok(dirname(anchor_file)))
        # dist_cls = PythonInstalledDistribution
        # package_type = PackageType.VIRTUAL_PYTHON_WHEEL
    elif anchor_file.endswith(".egg-info"):
        anchor_full_path = join(prefix_path, win_path_ok(anchor_file))
        sp_reference = basename(anchor_file)
        return PythonEggInfoDistribution(anchor_full_path, python_version, sp_reference)
        # sp_reference = basename(anchor_file)
        # dist_cls = PythonEggInfoDistribution
        # package_type = PackageType.VIRTUAL_PYTHON_EGG_UNMANAGEABLE
    elif ".egg-info" in anchor_file:
        anchor_full_path = join(prefix_path, win_path_ok(dirname(anchor_file)))
        sp_reference = basename(dirname(anchor_file))
        return PythonEggInfoDistribution(anchor_full_path, python_version, sp_reference)
        # sp_reference = basename(dirname(anchor_file))
        # dist_file = join(prefix_path, win_path_ok(dirname(anchor_file)))
        # dist_cls = PythonEggInfoDistribution
        # package_type = PackageType.VIRTUAL_PYTHON_EGG_MANAGEABLE
    elif ".egg" in anchor_file:
        anchor_full_path = join(prefix_path, win_path_ok(dirname(anchor_file)))
        sp_reference = basename(dirname(anchor_file))
        return PythonEggInfoDistribution(anchor_full_path, python_version, sp_reference)
        # sp_reference = basename(dirname(anchor_file))
        # dist_file = join(prefix_path, win_path_ok(dirname(anchor_file)))
        # dist_cls = PythonEggInfoDistribution
        # package_type = PackageType.VIRTUAL_PYTHON_EGG_MANAGEABLE
    else:
        raise NotImplementedError()

    # pydist = None
    #
    # # An egg-link might reference a folder where egg-info is not available
    # if dist_file is not None:
    #     pydist = dist_cls(dist_file)
    #     try:
    #         pydist = dist_cls(dist_file)
    #     except Exception as error:
    #         print('ERROR!: get_python_distribution_info', error)
    #
    # return pydist, sp_reference, package_type