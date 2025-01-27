#
# Copyright (c) YugaByte, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations
# under the License.
#

import os
import sys

from yugabyte_db_thirdparty.build_definition_helpers import *  # noqa


PROJECT_CONFIG = """
libraries = {5} ;

using {0} : {1} :
    {2} :
    {3}
    {4} ;
"""


class BoostDependency(Dependency):
    def __init__(self) -> None:
        super(BoostDependency, self).__init__(
            name='boost',
            version='1.69.0',
            # URL grabbed from https://www.boost.org/users/history/version_1_69_0.html
            url_pattern='https://boostorg.jfrog.io/artifactory/main/release/1.69.0/source/'
                        'boost_1_69_0.tar.bz2',
            build_group=BUILD_GROUP_INSTRUMENTED,
            license='Boost Software License 1.0')
        self.dir = '{}_{}'.format(self.name, self.underscored_version)
        self.copy_sources = True
        self.patches = ['boost-1-69-remove-pending-integer_log2-include.patch',
                        'boost-1-69-mac-compiler-flags.patch']
        self.patch_strip = 1

    def build(self, builder: BuilderInterface) -> None:
        libs = ['system', 'thread', 'atomic']

        log_prefix = builder.log_prefix(self)
        prefix = self.get_install_prefix(builder)
        log_output(log_prefix, ['./bootstrap.sh', '--prefix={}'.format(builder.prefix)])
        project_config = 'project-config.jam'
        with open(project_config, 'rt') as inp:
            original_lines = inp.readlines()
        with open(project_config, 'wt') as out:
            for line in original_lines:
                lstripped = line.lstrip()
                if not lstripped.startswith('libraries =') and \
                   not lstripped.startswith('using gcc ;') and \
                   not lstripped.startswith('project : default-build <toolset>gcc ;'):
                    out.write(line)
            cxx_flags = builder.compiler_flags + builder.cxx_flags
            compiler_type = builder.compiler_choice.compiler_type
            compiler_version = ''
            if compiler_type == 'gcc8':
                compiler_type = 'gcc'
                compiler_version = '8'
            out.write(PROJECT_CONFIG.format(
                    compiler_type,
                    compiler_version,
                    builder.compiler_choice.get_cxx_compiler(),
                    ' '.join(['<compileflags>' + flag for flag in cxx_flags]),
                    ' '.join(['<linkflags>' + flag for flag in cxx_flags + builder.ld_flags]),
                    ' '.join(['--with-{}'.format(lib) for lib in libs])))
        log_output(log_prefix, ['./b2', 'install', 'cxxstd=14'])

        if is_macos():
            for lib in libs:
                path = os.path.join(builder.prefix_lib, self.libfile(lib, builder))
                log_output(log_prefix, ['install_name_tool', '-id', path, path])
                for sublib in libs:
                    sublib_file = self.libfile(sublib, builder)
                    sublib_path = os.path.join(builder.prefix_lib, sublib_file)
                    log_output(log_prefix, ['install_name_tool', '-change', sublib_file,
                                            sublib_path, path])

    def libfile(self, lib: str, builder: BuilderInterface) -> str:
        return 'libboost_{}.{}'.format(lib, builder.dylib_suffix)
