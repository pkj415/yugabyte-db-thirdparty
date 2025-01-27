# Copyright (c) Yugabyte, Inc.
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

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import ruamel.yaml as ruamel_yaml  # type: ignore
from typing import Optional, List, Set, Tuple, Dict, Any

from sys_detection import is_macos, is_linux

from build_definitions import (
    BUILD_GROUP_COMMON,
    BUILD_GROUP_INSTRUMENTED,
    BUILD_TYPE_ASAN,
    BUILD_TYPE_COMMON,
    BUILD_TYPE_TSAN,
    BUILD_TYPE_UNINSTRUMENTED,
    BUILD_TYPES,
    get_build_def_module,
)
from yugabyte_db_thirdparty.builder_helpers import PLACEHOLDER_RPATH, get_make_parallelism, \
    get_rpath_flag, sanitize_flags_line_for_log, log_and_set_env_var_to_list
from yugabyte_db_thirdparty.builder_helpers import is_ninja_available
from yugabyte_db_thirdparty.builder_interface import BuilderInterface
from yugabyte_db_thirdparty.cmd_line_args import parse_cmd_line_args
from yugabyte_db_thirdparty.compiler_choice import CompilerChoice
from yugabyte_db_thirdparty.custom_logging import fatal, log, heading, log_output, colored_log, \
    YELLOW_COLOR, SEPARATOR
from yugabyte_db_thirdparty.dependency import Dependency
from yugabyte_db_thirdparty.devtoolset import activate_devtoolset
from yugabyte_db_thirdparty.download_manager import DownloadManager
from yugabyte_db_thirdparty.env_helpers import write_env_vars
from yugabyte_db_thirdparty.string_util import indent_lines
from yugabyte_db_thirdparty.util import (
    assert_dir_exists,
    assert_list_contains,
    EnvVarContext,
    mkdir_if_missing,
    PushDir,
    read_file,
    write_file,
    remove_path,
    YB_THIRDPARTY_DIR,
)
from yugabyte_db_thirdparty.file_system_layout import FileSystemLayout
from yugabyte_db_thirdparty.toolchain import Toolchain, ensure_toolchain_installed
from yugabyte_db_thirdparty.clang_util import get_clang_library_dir


ASAN_FLAGS = [
    '-fsanitize=address',
    '-fsanitize=undefined',
    '-DADDRESS_SANITIZER',
]

TSAN_FLAGS = [
    '-fsanitize=thread',
    '-DTHREAD_SANITIZER',
]


class Builder(BuilderInterface):
    args: argparse.Namespace
    ld_flags: List[str]
    executable_only_ld_flags: List[str]
    compiler_flags: List[str]
    preprocessor_flags: List[str]
    c_flags: List[str]
    cxx_flags: List[str]
    libs: List[str]
    additional_allowed_shared_lib_paths: Set[str]
    download_manager: DownloadManager
    compiler_choice: CompilerChoice
    fs_layout: FileSystemLayout
    fossa_modules: List[Any]
    toolchain: Optional[Toolchain]
    remote_build: bool

    """
    This class manages the overall process of building third-party dependencies, including the set
    of dependencies to build, build types, and the directories to install dependencies.
    """
    def __init__(self) -> None:
        self.fs_layout = FileSystemLayout()
        self.linuxbrew_dir = None
        self.additional_allowed_shared_lib_paths = set()

        self.toolchain = None
        self.fossa_modules = []

    def parse_args(self) -> None:
        self.args = parse_cmd_line_args()

        self.remote_build = self.args.remote_build_server and self.args.remote_build_dir
        if self.remote_build:
            return

        if self.args.make_parallelism:
            os.environ['YB_MAKE_PARALLELISM'] = str(self.args.make_parallelism)

        self.download_manager = DownloadManager(
            should_add_checksum=self.args.add_checksum,
            download_dir=self.fs_layout.tp_download_dir)

        single_compiler_type = None
        if self.args.toolchain:
            self.toolchain = ensure_toolchain_installed(
                self.download_manager, self.args.toolchain)
            compiler_prefix = self.toolchain.toolchain_root
            single_compiler_type = self.toolchain.get_compiler_type()
            self.toolchain.write_url_and_path_files()
        else:
            compiler_prefix = self.args.compiler_prefix
            single_compiler_type = self.args.single_compiler_type

        self.compiler_choice = CompilerChoice(
            single_compiler_type=single_compiler_type,
            compiler_prefix=compiler_prefix,
            compiler_suffix=self.args.compiler_suffix,
            devtoolset=self.args.devtoolset,
            use_compiler_wrapper=self.args.use_compiler_wrapper,
            use_ccache=self.args.use_ccache,
            expected_major_compiler_version=self.args.expected_major_compiler_version
        )

    def finish_initialization(self) -> None:
        self.compiler_choice.finish_initialization()
        self.populate_dependencies()
        self.select_dependencies_to_build()
        if self.compiler_choice.devtoolset is not None:
            activate_devtoolset(self.compiler_choice.devtoolset)

    def populate_dependencies(self) -> None:
        # We have to use get_build_def_module to access submodules of build_definitions,
        # otherwise MyPy gets confused.

        self.dependencies = [
            # Avoiding a name collision with the standard zlib module, hence "zlib_dependency".
            get_build_def_module('zlib_dependency').ZLibDependency(),
            get_build_def_module('lz4').LZ4Dependency(),
            get_build_def_module('openssl').OpenSSLDependency(),
            get_build_def_module('libev').LibEvDependency(),
            get_build_def_module('rapidjson').RapidJsonDependency(),
            get_build_def_module('squeasel').SqueaselDependency(),
            get_build_def_module('curl').CurlDependency(),
            get_build_def_module('hiredis').HiRedisDependency(),
            get_build_def_module('cqlsh').CQLShDependency(),
            get_build_def_module('redis_cli').RedisCliDependency(),
            get_build_def_module('flex').FlexDependency(),
            get_build_def_module('bison').BisonDependency(),
            get_build_def_module('libedit').LibEditDependency(),
            get_build_def_module('openldap').OpenLDAPDependency(),
        ]

        if is_linux():
            self.dependencies += [
                get_build_def_module('libuuid').LibUuidDependency(),
            ]

            standalone_llvm7_toolchain = self.toolchain and self.toolchain.toolchain_type == 'llvm7'
            if standalone_llvm7_toolchain:
                self.dependencies.append(
                        get_build_def_module('llvm7_libcxx').Llvm7LibCXXDependency())

            llvm_major_version: Optional[int] = self.compiler_choice.get_llvm_major_version()
            if (self.compiler_choice.use_only_clang() and
                    llvm_major_version is not None and llvm_major_version >= 10):
                llvm_version_str = self.compiler_choice.get_llvm_version_str()
                self.dependencies += [
                    # New LLVM. We will keep supporting new LLVM versions here.
                    get_build_def_module('llvm1x_libunwind').Llvm1xLibUnwindDependency(
                        version=llvm_version_str
                    ),
                    get_build_def_module('llvm1x_libcxx').Llvm1xLibCxxAbiDependency(
                        version=llvm_version_str
                    ),
                    get_build_def_module('llvm1x_libcxx').Llvm1xLibCxxDependency(
                        version=llvm_version_str
                    ),
                ]
            else:
                self.dependencies.append(get_build_def_module('libunwind').LibUnwindDependency())

            self.dependencies.append(get_build_def_module('libbacktrace').LibBacktraceDependency())

        self.dependencies += [
            get_build_def_module('icu4c').Icu4cDependency(),
            get_build_def_module('protobuf').ProtobufDependency(),
            get_build_def_module('crypt_blowfish').CryptBlowfishDependency(),
            get_build_def_module('boost').BoostDependency(),

            get_build_def_module('gflags').GFlagsDependency(),
            get_build_def_module('glog').GLogDependency(),
            get_build_def_module('gperftools').GPerfToolsDependency(),
            get_build_def_module('gmock').GMockDependency(),
            get_build_def_module('snappy').SnappyDependency(),
            get_build_def_module('crcutil').CRCUtilDependency(),
            get_build_def_module('libcds').LibCDSDependency(),

            get_build_def_module('libuv').LibUvDependency(),
            get_build_def_module('cassandra_cpp_driver').CassandraCppDriverDependency(),
        ]

    def select_dependencies_to_build(self) -> None:
        self.selected_dependencies = []
        if self.args.dependencies:
            names = set([dep.name for dep in self.dependencies])
            for dep in self.args.dependencies:
                if dep not in names:
                    fatal("Unknown dependency name: %s. Valid dependency names:\n%s",
                          dep,
                          (" " * 4 + ("\n" + " " * 4).join(sorted(names))))
            for dep in self.dependencies:
                if dep.name in self.args.dependencies:
                    self.selected_dependencies.append(dep)
        elif self.args.skip:
            skipped = set(self.args.skip.split(','))
            log("Skipping dependencies: %s", sorted(skipped))
            self.selected_dependencies = []
            for dependency in self.dependencies:
                if dependency.name in skipped:
                    skipped.remove(dependency.name)
                else:
                    self.selected_dependencies.append(dependency)
            if skipped:
                raise ValueError("Unknown dependencies, cannot skip: %s" % sorted(skipped))
        else:
            self.selected_dependencies = self.dependencies

    def run(self) -> None:
        self.compiler_choice.set_compiler(
            'clang' if self.compiler_choice.use_only_clang() else 'gcc')
        if self.args.clean or self.args.clean_downloads:
            self.fs_layout.clean(self.selected_dependencies, self.args.clean_downloads)
        self.prepare_out_dirs()
        os.environ['PATH'] = ':'.join([
                os.path.join(self.fs_layout.tp_installed_common_dir, 'bin'),
                os.path.join(self.fs_layout.tp_installed_llvm7_common_dir, 'bin'),
                os.environ['PATH']
        ])

        self.build_one_build_type(BUILD_TYPE_COMMON)
        build_types = [BUILD_TYPE_UNINSTRUMENTED]

        if is_linux() and self.compiler_choice.use_only_clang() and not self.args.skip_sanitizers:
            # We only support ASAN/TSAN builds on Clang.
            build_types.append(BUILD_TYPE_ASAN)
            build_types.append(BUILD_TYPE_TSAN)
        log(f"Full list of build types: {build_types}")

        for build_type in build_types:
            self.build_one_build_type(build_type)

        yaml = ruamel_yaml.YAML(typ='safe', pure=True)
        with open(os.path.join(YB_THIRDPARTY_DIR, 'fossa_modules.yml'), 'w') as output_file:
            yaml.dump(self.fossa_modules, output_file)

    def get_build_types(self) -> List[str]:
        return list(BUILD_TYPES)

    def prepare_out_dirs(self) -> None:
        build_types = self.get_build_types()
        dirs = [
            os.path.join(self.fs_layout.tp_installed_dir, build_type) for build_type in build_types
        ]
        libcxx_dirs = [os.path.join(dir, 'libcxx') for dir in dirs]
        for dir in dirs + libcxx_dirs:
            if self.args.verbose:
                log("Preparing output directory %s", dir)
            lib_dir = os.path.join(dir, 'lib')
            mkdir_if_missing(lib_dir)
            mkdir_if_missing(os.path.join(dir, 'include'))
            # On some systems, autotools installs libraries to lib64 rather than lib.    Fix
            # this by setting up lib64 as a symlink to lib.    We have to do this step first
            # to handle cases where one third-party library depends on another.    Make sure
            # we create a relative symlink so that the entire PREFIX_DIR could be moved,
            # e.g. after it is packaged and then downloaded on a different build node.
            lib64_dir = os.path.join(dir, 'lib64')
            if os.path.exists(lib64_dir):
                if os.path.islink(lib64_dir):
                    continue
                remove_path(lib64_dir)
            os.symlink('lib', lib64_dir)

    def add_include_path(self, include_path: str) -> None:
        if self.args.verbose:
            log("Adding an include path: %s", include_path)
        cmd_line_arg = f'-I{include_path}'
        self.preprocessor_flags.append(cmd_line_arg)
        self.compiler_flags.append(cmd_line_arg)

    def init_compiler_independent_flags(self, dep: Dependency) -> None:
        """
        Initialize compiler and linker flags for a particular build type. We try to limit this
        function to flags that will work for most compilers we are using, which include various
        versions of GCC and Clang.
        """
        self.preprocessor_flags = []
        self.ld_flags = []
        self.executable_only_ld_flags = []
        self.compiler_flags = []
        self.c_flags = []
        self.cxx_flags = []
        self.libs = []

        self.add_linuxbrew_flags()
        for include_dir_component in set([BUILD_TYPE_COMMON, self.build_type]):
            self.add_include_path(os.path.join(
                self.fs_layout.tp_installed_dir, include_dir_component, 'include'))
            self.add_lib_dir_and_rpath(os.path.join(
                self.fs_layout.tp_installed_dir, include_dir_component, 'lib'))

        self.compiler_flags += self.preprocessor_flags
        # -fPIC is there to always generate position-independent code, even for static libraries.
        self.compiler_flags += ['-fno-omit-frame-pointer', '-fPIC', '-O2', '-Wall']
        if is_linux():
            # On Linux, ensure we set a long enough rpath so we can change it later with chrpath,
            # patchelf, or a similar tool.
            self.add_rpath(PLACEHOLDER_RPATH)

            self.dylib_suffix = "so"
        elif is_macos():
            self.dylib_suffix = "dylib"

            # YugaByte builds with C++11, which on OS X requires using libc++ as the standard
            # library implementation. Some of the dependencies do not compile against libc++ by
            # default, so we specify it explicitly.
            self.cxx_flags.append("-stdlib=libc++")
            self.ld_flags += ["-lc++", "-lc++abi"]

            # Build for macOS Mojave or later. See https://bit.ly/37myHbk
            self.compiler_flags.append("-mmacosx-version-min=10.14")
            self.ld_flags.append("-Wl,-headerpad_max_install_names")
        else:
            fatal("Unsupported platform: {}".format(platform.system()))

        # The C++ standard must match CMAKE_CXX_STANDARD in the top-level CMakeLists.txt file in
        # the YugabyteDB source tree.
        self.cxx_flags.append('-std=c++14')
        self.cxx_flags.append('-frtti')

        if self.build_type == BUILD_TYPE_ASAN:
            self.compiler_flags += ASAN_FLAGS

        if self.build_type == BUILD_TYPE_TSAN:
            self.compiler_flags += TSAN_FLAGS

    def add_linuxbrew_flags(self) -> None:
        if self.compiler_choice.using_linuxbrew():
            lib_dir = os.path.join(self.compiler_choice.get_linuxbrew_dir(), 'lib')
            self.ld_flags.append(" -Wl,-dynamic-linker={}".format(os.path.join(lib_dir, 'ld.so')))
            self.add_lib_dir_and_rpath(lib_dir)

    def add_lib_dir_and_rpath(self, lib_dir: str) -> None:
        if self.args.verbose:
            log("Adding a library directory and RPATH at the end of linker flags: %s", lib_dir)
        self.ld_flags.append("-L{}".format(lib_dir))
        self.add_rpath(lib_dir)

    def prepend_lib_dir_and_rpath(self, lib_dir: str) -> None:
        if self.args.verbose:
            log("Adding a library directory and RPATH at the front of linker flags: %s", lib_dir)
        self.ld_flags.insert(0, "-L{}".format(lib_dir))
        self.prepend_rpath(lib_dir)

    def add_rpath(self, path: str) -> None:
        log("Adding RPATH at the end of linker flags: %s", path)
        self.ld_flags.append(get_rpath_flag(path))
        self.additional_allowed_shared_lib_paths.add(path)

    def prepend_rpath(self, path: str) -> None:
        log("Adding RPATH at the front of linker flags: %s", path)
        self.ld_flags.insert(0, get_rpath_flag(path))
        self.additional_allowed_shared_lib_paths.add(path)

    def log_prefix(self, dep: Dependency) -> str:
        return '{} ({})'.format(dep.name, self.build_type)

    def build_with_configure(
            self,
            log_prefix: str,
            extra_args: List[str] = [],
            configure_cmd: List[str] = ['./configure'],
            install: List[str] = ['install'],
            run_autogen: bool = False,
            autoconf: bool = False,
            src_subdir_name: Optional[str] = None) -> None:
        os.environ["YB_REMOTE_COMPILATION"] = "0"
        dir_for_build = os.getcwd()
        if src_subdir_name:
            dir_for_build = os.path.join(dir_for_build, src_subdir_name)

        with PushDir(dir_for_build):
            log("Building in %s using the configure tool", dir_for_build)
            try:
                if run_autogen:
                    log_output(log_prefix, ['./autogen.sh'])
                if autoconf:
                    log_output(log_prefix, ['autoreconf', '-i'])

                configure_args = (
                    configure_cmd.copy() + ['--prefix={}'.format(self.prefix)] + extra_args
                )
                log_output(log_prefix, configure_args)
            except Exception as ex:
                log(f"The configure step failed. Looking for relevant files in {dir_for_build} "
                    f"to show.")
                num_files_shown = 0
                for root, dirs, files in os.walk('.'):
                    for file_name in files:
                        if file_name == 'config.log':
                            file_path = os.path.abspath(os.path.join(root, file_name))
                            log(
                                f"Contents of {file_path}:\n"
                                f"\n"
                                f"{read_file(file_path)}\n"
                                f"\n"
                                f"(End of {file_path}).\n"
                                f"\n"
                            )
                            num_files_shown += 1
                log(f"Logged contents of {num_files_shown} relevant files in {dir_for_build}.")
                raise

            log_output(log_prefix, ['make', '-j{}'.format(get_make_parallelism())])
            if install:
                log_output(log_prefix, ['make'] + install)

    def build_with_cmake(
            self,
            dep: Dependency,
            extra_args: List[str] = [],
            use_ninja_if_available: bool = True,
            src_subdir_name: Optional[str] = None,
            extra_build_tool_args: List[str] = [],
            should_install: bool = True,
            install_targets: List[str] = ['install'],
            shared_and_static: bool = False) -> None:
        build_tool = 'make'
        if use_ninja_if_available:
            ninja_available = is_ninja_available()
            log('Ninja is %s', 'available' if ninja_available else 'unavailable')
            if ninja_available:
                build_tool = 'ninja'

        log("Building dependency %s using CMake. Build tool: %s", dep, build_tool)
        log_prefix = self.log_prefix(dep)
        os.environ["YB_REMOTE_COMPILATION"] = "0"

        remove_path('CMakeCache.txt')
        remove_path('CMakeFiles')

        src_path = self.fs_layout.get_source_path(dep)
        if src_subdir_name is not None:
            src_path = os.path.join(src_path, src_subdir_name)

        args = ['cmake', src_path]
        if build_tool == 'ninja':
            args += ['-G', 'Ninja']
        args += self.get_common_cmake_flag_args(dep)
        if extra_args is not None:
            args += extra_args
        args += dep.get_additional_cmake_args(self)

        if shared_and_static and any(arg.startswith('-DBUILD_SHARED_LIBS=') for arg in args):
            raise ValueError(
                "shared_and_static=True is specified but CMake arguments already mention "
                "-DBUILD_SHARED_LIBS: %s" % args)

        if '-DBUILD_SHARED_LIBS=OFF' not in args and not shared_and_static:
            # TODO: a better approach for setting CMake arguments from multiple places.
            args.append('-DBUILD_SHARED_LIBS=ON')

        def build_internal(even_more_cmake_args: List[str] = []) -> None:
            final_cmake_args = args + even_more_cmake_args
            log("CMake command line (one argument per line):\n%s" %
                "\n".join([(" " * 4 + sanitize_flags_line_for_log(line))
                           for line in final_cmake_args]))
            log_output(log_prefix, final_cmake_args)

            if build_tool == 'ninja':
                dep.postprocess_ninja_build_file(self, 'build.ninja')

            build_tool_cmd = [
                build_tool, '-j{}'.format(get_make_parallelism())
            ] + extra_build_tool_args

            log_output(log_prefix, build_tool_cmd)

            if should_install:
                log_output(log_prefix, [build_tool] + install_targets)

            with open('compile_commands.json') as compile_commands_file:
                compile_commands = json.load(compile_commands_file)

            for command_item in compile_commands:
                command_args = command_item['command'].split()
                if self.build_type == BUILD_TYPE_ASAN:
                    assert_list_contains(command_args, '-fsanitize=address')
                    assert_list_contains(command_args, '-fsanitize=undefined')
                if self.build_type == BUILD_TYPE_TSAN:
                    assert_list_contains(command_args, '-fsanitize=thread')

        if shared_and_static:
            for build_shared_libs_value, subdir_name in (
                ('ON', 'shared'),
                ('OFF', 'static')
            ):
                build_dir = os.path.join(os.getcwd(), subdir_name)
                mkdir_if_missing(build_dir)
                build_shared_libs_cmake_arg = '-DBUILD_SHARED_LIBS=%s' % build_shared_libs_value
                log("Building dependency '%s' for build type '%s' with option: %s",
                    dep.name, self.build_type, build_shared_libs_cmake_arg)
                with PushDir(build_dir):
                    build_internal([build_shared_libs_cmake_arg])
        else:
            build_internal()

    def build_one_build_type(self, build_type: str) -> None:
        if (build_type != BUILD_TYPE_COMMON and
                self.args.build_type is not None and
                build_type != self.args.build_type):
            log("Skipping build type %s because build type %s is specified in the arguments",
                build_type, self.args.build_type)
            return

        self.set_build_type(build_type)
        build_group = (
            BUILD_GROUP_COMMON if build_type == BUILD_TYPE_COMMON else BUILD_GROUP_INSTRUMENTED
        )

        for dep in self.selected_dependencies:
            if build_group == dep.build_group:
                self.perform_pre_build_steps(dep)
                should_build = dep.should_build(self)
                should_rebuild = self.should_rebuild_dependency(dep)
                if should_build and should_rebuild:
                    self.build_dependency(dep, only_process_flags=False)
                else:
                    self.build_dependency(dep, only_process_flags=True)
                    log(f"Skipped dependency {dep.name}: "
                        f"should_build={should_build}, "
                        f"should_rebuild={should_rebuild}.")

    def get_install_prefix_with_qualifier(self, qualifier: Optional[str] = None) -> str:
        return os.path.join(
            self.fs_layout.tp_installed_dir,
            self.build_type + ('_%s' % qualifier if qualifier else ''))

    def set_build_type(self, build_type: str) -> None:
        self.build_type = build_type
        self.prefix = self.get_install_prefix_with_qualifier(qualifier=None)
        self.prefix_bin = os.path.join(self.prefix, 'bin')
        self.prefix_lib = os.path.join(self.prefix, 'lib')
        self.prefix_include = os.path.join(self.prefix, 'include')
        if self.compiler_choice.building_with_clang(build_type):
            compiler = 'clang'
        else:
            compiler = 'gcc'
        self.compiler_choice.set_compiler(compiler)
        heading("Building {} dependencies (compiler type: {})".format(
            build_type, self.compiler_choice.compiler_type))
        log("Compiler type: %s", self.compiler_choice.compiler_type)
        log("C compiler: %s", self.compiler_choice.get_c_compiler())
        log("C++ compiler: %s", self.compiler_choice.get_cxx_compiler())

    def init_flags(self, dep: Dependency) -> None:
        """
        Initializes compiler and linker flags. No flag customizations should be transferred from one
        dependency to another.
        """
        self.init_compiler_independent_flags(dep)

        if not is_macos() and self.compiler_choice.building_with_clang(self.build_type):
            # Special setup for Clang on Linux.
            compiler_choice = self.compiler_choice
            llvm_major_version: Optional[int] = compiler_choice.get_llvm_major_version()
            if (compiler_choice.single_compiler_type == 'clang' and
                    llvm_major_version is not None and llvm_major_version >= 10):
                # We are assuming that --single-compiler-type will only be used for Clang 10 and
                # newer.
                self.init_linux_clang1x_flags(dep)
            elif llvm_major_version == 7 or compiler_choice.single_compiler_type is None:
                # We are either building with LLVM 7 without Linuxbrew, or this is the
                # Linuxbrew-based build with both GCC and Clang (which will go away).
                self.init_linux_clang7_flags(dep)
            else:
                raise ValueError(f"Unsupported LLVM major version: {llvm_major_version}")

    def get_libcxx_dirs(self, libcxx_installed_suffix: str) -> Tuple[str, str]:
        libcxx_installed_path = os.path.join(
            self.fs_layout.tp_installed_dir, libcxx_installed_suffix, 'libcxx')
        libcxx_installed_include = os.path.join(libcxx_installed_path, 'include', 'c++', 'v1')
        libcxx_installed_lib = os.path.join(libcxx_installed_path, 'lib')
        return libcxx_installed_include, libcxx_installed_lib

    def init_linux_clang7_flags(self, dep: Dependency) -> None:
        """
        Flags used to build code with Clang 7 that we build here. As we move to newer versions of
        Clang, this function will go away.
        """
        if self.build_type == BUILD_TYPE_TSAN:
            # Ensure that TSAN runtime is linked statically into every executable. TSAN runtime
            # uses -fPIE while our shared libraries use -fPIC, and therefore TSAN runtime can only
            # be linked statically into executables. TSAN runtime can't be built with -fPIC because
            # that would create significant performance issues.
            self.executable_only_ld_flags += ['-fsanitize=thread']

        # This is used to build code with libc++ and Clang 7 built as part of thirdparty.
        stdlib_suffix = self.build_type
        stdlib_path = os.path.join(self.fs_layout.tp_installed_dir, stdlib_suffix, 'libcxx')
        stdlib_include = os.path.join(stdlib_path, 'include', 'c++', 'v1')
        stdlib_lib = os.path.join(stdlib_path, 'lib')
        self.cxx_flags.insert(0, '-nostdinc++')
        self.cxx_flags.insert(0, '-isystem')
        self.cxx_flags.insert(1, stdlib_include)
        self.cxx_flags.insert(0, '-stdlib=libc++')
        # Clang complains about argument unused during compilation: '-stdlib=libc++' when both
        # -stdlib=libc++ and -nostdinc++ are specified.
        self.cxx_flags.insert(0, '-Wno-error=unused-command-line-argument')
        self.prepend_lib_dir_and_rpath(stdlib_lib)
        if self.compiler_choice.using_linuxbrew():
            self.compiler_flags.append('--gcc-toolchain={}'.format(
                self.compiler_choice.get_linuxbrew_dir()))

        if self.toolchain and self.toolchain.toolchain_type == 'llvm7':
            # This is needed when building with Clang 7 but without Linuxbrew.
            # TODO: this might only be needed due to using an old version of libunwind that is
            # different from libunwind included in the LLVM 7 repository. Just a hypothesis.
            self.ld_flags.append('-lgcc_s')

    def init_linux_clang1x_flags(self, dep: Dependency) -> None:
        """
        Flags for Clang 10 and beyond. We are using LLVM-supplied libunwind and compiler-rt in this
        configuration.
        """
        self.ld_flags.append('-rtlib=compiler-rt')

        if self.build_type == BUILD_TYPE_COMMON:
            log("Not configuring any special Clang 10+ flags for build type %s", self.build_type)
            return

        # TODO mbautin: refactor to polymorphism
        is_libcxxabi = dep.name.endswith('_libcxxabi')
        is_libcxx = dep.name.endswith('_libcxx')
        log("Dependency name: %s, is_libcxxabi: %s, is_libcxx: %s",
            dep.name, is_libcxxabi, is_libcxx)

        if self.build_type == BUILD_TYPE_ASAN:
            self.compiler_flags.append('-shared-libasan')

            if is_libcxxabi:
                # To avoid an infinite loop in UBSAN.
                # https://monorail-prod.appspot.com/p/chromium/issues/detail?id=609786
                # This comment:
                # https://gist.githubusercontent.com/mbautin/ad9ea4715669da3b3a5fb9495659c4a9/raw
                self.compiler_flags.append('-fno-sanitize=vptr')

            assert self.compiler_choice.cc is not None
            compiler_rt_lib_dir = get_clang_library_dir(self.compiler_choice.cc)
            self.add_lib_dir_and_rpath(compiler_rt_lib_dir)
            ubsan_lib_name = f'clang_rt.ubsan_minimal-{platform.processor()}'
            ubsan_lib_so_path = os.path.join(compiler_rt_lib_dir, f'lib{ubsan_lib_name}.so')
            if not os.path.exists(ubsan_lib_so_path):
                raise IOError(f"UBSAN library not found at {ubsan_lib_so_path}")
            self.ld_flags.append(f'-l{ubsan_lib_name}')

        self.ld_flags += ['-lunwind']

        libcxx_installed_include, libcxx_installed_lib = self.get_libcxx_dirs(self.build_type)
        log("libc++ include directory: %s", libcxx_installed_include)
        log("libc++ library directory: %s", libcxx_installed_lib)

        if not is_libcxx and not is_libcxxabi:
            log("Adding special compiler/linker flags for Clang 10+ for dependencies other than "
                "libc++")
            self.ld_flags += ['-lc++', '-lc++abi']

            self.cxx_flags = [
                '-stdlib=libc++',
                '-isystem',
                libcxx_installed_include,
                '-nostdinc++'
            ] + self.cxx_flags
            self.prepend_lib_dir_and_rpath(libcxx_installed_lib)

        if is_libcxx:
            log("Adding special compiler/linker flags for Clang 10 or newer for libc++")
            # This is needed for libc++ to find libc++abi headers.
            assert_dir_exists(libcxx_installed_include)
            self.cxx_flags.append('-I%s' % libcxx_installed_include)
            # libc++ build needs to be able to find libc++abi library installed here.
            self.ld_flags.append('-L%s' % libcxx_installed_lib)

        if is_libcxx or is_libcxxabi:
            log("Adding special linker flags for Clang 10 or newer for libc++ or libc++abi")
            # libc++abi needs to be able to find libcxx at runtime, even though it can't always find
            # it at build time because libc++abi is built first.
            self.add_rpath(libcxx_installed_lib)

        self.cxx_flags.append('-Wno-error=unused-command-line-argument')
        log("Flags after the end of setup for Clang 10 or newer:")
        log("cxx_flags : %s", self.cxx_flags)
        log("c_flags   : %s", self.c_flags)
        log("ld_flags  : %s", self.ld_flags)

    def get_effective_compiler_flags(self, dep: Dependency) -> List[str]:
        return self.compiler_flags + dep.get_additional_compiler_flags(self)

    def get_effective_cxx_flags(self, dep: Dependency) -> List[str]:
        return (self.cxx_flags +
                self.get_effective_compiler_flags(dep) +
                dep.get_additional_cxx_flags(self))

    def get_effective_c_flags(self, dep: Dependency) -> List[str]:
        return (self.c_flags +
                self.get_effective_compiler_flags(dep) +
                dep.get_additional_c_flags(self))

    def get_effective_ld_flags(self, dep: Dependency) -> List[str]:
        return self.ld_flags + dep.get_additional_ld_flags(self)

    def get_effective_executable_ld_flags(self, dep: Dependency) -> List[str]:
        return self.ld_flags + self.executable_only_ld_flags + dep.get_additional_ld_flags(self)

    def get_effective_preprocessor_flags(self, dep: Dependency) -> List[str]:
        return list(self.preprocessor_flags)

    def get_common_cmake_flag_args(self, dep: Dependency) -> List[str]:
        c_flags_str = ' '.join(self.get_effective_c_flags(dep))
        cxx_flags_str = ' '.join(self.get_effective_cxx_flags(dep))

        # TODO: we are not using this. What is the best way to plug this into CMake?
        preprocessor_flags_str = ' '.join(self.get_effective_preprocessor_flags(dep))

        ld_flags_str = ' '.join(self.get_effective_ld_flags(dep))
        exe_ld_flags_str = ' '.join(self.get_effective_executable_ld_flags(dep))
        return [
            '-DCMAKE_C_FLAGS={}'.format(c_flags_str),
            '-DCMAKE_CXX_FLAGS={}'.format(cxx_flags_str),
            '-DCMAKE_SHARED_LINKER_FLAGS={}'.format(ld_flags_str),
            '-DCMAKE_EXE_LINKER_FLAGS={}'.format(exe_ld_flags_str),
            '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
            '-DCMAKE_INSTALL_PREFIX={}'.format(dep.get_install_prefix(self)),
            '-DCMAKE_POSITION_INDEPENDENT_CODE=ON'
        ]

    def perform_pre_build_steps(self, dep: Dependency) -> None:
        log("")
        colored_log(YELLOW_COLOR, SEPARATOR)
        colored_log(YELLOW_COLOR, "Building %s (%s)", dep.name, self.build_type)
        colored_log(YELLOW_COLOR, SEPARATOR)

        self.download_manager.download_dependency(
            dep=dep,
            src_path=self.fs_layout.get_source_path(dep),
            archive_path=self.fs_layout.get_archive_path(dep))

        archive_name = dep.get_archive_name()
        if archive_name:
            archive_path = os.path.join('downloads', archive_name)
            self.fossa_modules.append({
                "fossa_module": {
                    "name": f"{dep.name}-{dep.version}",
                    "type": "raw",
                    "target": os.path.basename(archive_path)
                },
                "yb_metadata": {
                    "url": dep.download_url,
                    "sha256sum": self.download_manager.get_expected_checksum(archive_name)
                }
            })

    def build_dependency(self, dep: Dependency, only_process_flags: bool = False) -> None:
        """
        Build the given dependency.

        :param only_process_flags: if this is True, we will only set up the compiler and linker
            flags and apply all the side effects of that process, such as collecting the set of
            allowed library paths referred by the final artifacts. If False, we will actually do
            the build.
        """

        self.init_flags(dep)

        # This is needed at least for glog to be able to find gflags.
        self.add_rpath(os.path.join(self.fs_layout.tp_installed_dir, self.build_type, 'lib'))

        if self.build_type != BUILD_TYPE_COMMON:
            # Needed to find libunwind for Clang 10 when using compiler-rt.
            self.add_rpath(os.path.join(self.fs_layout.tp_installed_dir, BUILD_TYPE_COMMON, 'lib'))

        if only_process_flags:
            log("Skipping the build of dependecy %s", dep.name)
            return

        if self.args.download_extract_only:
            log("Skipping build of dependency %s, build type %s, --download-extract-only is "
                "specified.", dep.name, self.build_type)
            return

        env_vars: Dict[str, Optional[str]] = {
            "CPPFLAGS": " ".join(self.preprocessor_flags)
        }

        log_and_set_env_var_to_list(env_vars, 'CXXFLAGS', self.get_effective_cxx_flags(dep))
        log_and_set_env_var_to_list(env_vars, 'CFLAGS', self.get_effective_c_flags(dep))
        log_and_set_env_var_to_list(env_vars, 'LDFLAGS', self.get_effective_ld_flags(dep))
        log_and_set_env_var_to_list(env_vars, 'LIBS', self.libs)
        log_and_set_env_var_to_list(
            env_vars, 'CPPFLAGS', self.get_effective_preprocessor_flags(dep))

        if self.build_type == BUILD_TYPE_ASAN:
            # To avoid errors similar to:
            # https://gist.githubusercontent.com/mbautin/4b8eec566f54bcc35706dcd97cab1a95/raw
            #
            # This could also be fixed to some extent by the compiler flags
            # -mllvm -asan-use-private-alias=1
            # but applying that flag to all builds is complicated in practice and is probably
            # best done using a compiler wrapper script, which would slow things down.
            #
            # Also do not detect memory leaks during the build process. E.g. configure scripts might
            # create some programs that have memory leaks and the configure process would fail.
            env_vars["ASAN_OPTIONS"] = ':'.join(["detect_odr_violation=0", "detect_leaks=0"])

        with PushDir(self.create_build_dir_and_prepare(dep)):
            with EnvVarContext(**env_vars):
                write_env_vars('yb_dependency_env.sh')
                dep.build(self)
        self.save_build_stamp_for_dependency(dep)
        log("")
        log("Finished building %s (%s)", dep.name, self.build_type)
        log("")

    # Determines if we should rebuild a component with the given name based on the existing "stamp"
    # file and the current value of the "stamp" (based on Git SHA1 and local changes) for the
    # component. The result is returned in should_rebuild_component_rv variable, which should have
    # been made local by the caller.
    def should_rebuild_dependency(self, dep: Dependency) -> bool:
        stamp_path = self.fs_layout.get_build_stamp_path_for_dependency(dep, self.build_type)
        old_build_stamp = None
        if os.path.exists(stamp_path):
            with open(stamp_path, 'rt') as inp:
                old_build_stamp = inp.read()

        new_build_stamp = self.get_build_stamp_for_dependency(dep)

        if dep.dir_name is not None:
            src_dir = self.fs_layout.get_source_path(dep)
            if not os.path.exists(src_dir):
                log("Have to rebuild %s (%s): source dir %s does not exist",
                    dep.name, self.build_type, src_dir)
                return True

        if old_build_stamp == new_build_stamp:
            log("Not rebuilding %s (%s) -- nothing changed.", dep.name, self.build_type)
            return False

        log("Have to rebuild %s (%s):", dep.name, self.build_type)
        log("Old build stamp for %s (from %s):\n%s",
            dep.name, stamp_path, indent_lines(old_build_stamp))
        log("New build stamp for %s:\n%s",
            dep.name, indent_lines(new_build_stamp))
        return True

    # Come up with a string that allows us to tell when to rebuild a particular third-party
    # dependency. The result is returned in the get_build_stamp_for_component_rv variable, which
    # should have been made local by the caller.
    def get_build_stamp_for_dependency(self, dep: Dependency) -> str:
        module_name = dep.__class__.__module__
        assert isinstance(module_name, str), "Dependency's module is not a string: %s" % module_name
        assert module_name.startswith('build_definitions.'), "Invalid module name: %s" % module_name
        module_name_components = module_name.split('.')
        assert len(module_name_components) == 2, (
                "Expected two components: %s" % module_name_components)
        module_name_final = module_name_components[-1]
        input_files_for_stamp = [
            'python/yugabyte_db_thirdparty/yb_build_thirdparty_main.py',
            'build_thirdparty.sh',
            os.path.join('python', 'build_definitions', '%s.py' % module_name_final)
        ]

        for path in input_files_for_stamp:
            abs_path = os.path.join(YB_THIRDPARTY_DIR, path)
            if not os.path.exists(abs_path):
                fatal("File '%s' does not exist -- expecting it to exist when creating a 'stamp' "
                      "for the build configuration of '%s'.", abs_path, dep.name)

        with PushDir(YB_THIRDPARTY_DIR):
            git_commit_sha1 = subprocess.check_output(
                ['git', 'log', '--pretty=%H', '-n', '1'] + input_files_for_stamp
            ).strip().decode('utf-8')
            build_stamp = 'git_commit_sha1={}\n'.format(git_commit_sha1)
            for git_extra_arg in (None, '--cached'):
                git_extra_args = [git_extra_arg] if git_extra_arg else []
                git_diff = subprocess.check_output(
                    ['git', 'diff'] + git_extra_args + input_files_for_stamp)
                git_diff_sha256 = hashlib.sha256(git_diff).hexdigest()
                build_stamp += 'git_diff_sha256{}={}\n'.format(
                    '_'.join(git_extra_args).replace('--', '_'),
                    git_diff_sha256)
            return build_stamp

    def save_build_stamp_for_dependency(self, dep: Dependency) -> None:
        stamp = self.get_build_stamp_for_dependency(dep)
        stamp_path = self.fs_layout.get_build_stamp_path_for_dependency(dep, self.build_type)

        log("Saving new build stamp to '%s':\n%s", stamp_path, indent_lines(stamp))
        with open(stamp_path, "wt") as out:
            out.write(stamp)

    def create_build_dir_and_prepare(self, dep: Dependency) -> str:
        src_dir = self.fs_layout.get_source_path(dep)
        if not os.path.isdir(src_dir):
            fatal("Directory '{}' does not exist".format(src_dir))

        build_dir = self.fs_layout.get_build_dir_for_dependency(dep, self.build_type)
        mkdir_if_missing(build_dir)

        if dep.copy_sources:
            log("Bootstrapping %s from %s", build_dir, src_dir)
            subprocess.check_call(['rsync', '-a', src_dir + '/', build_dir])
        return build_dir

    def is_release_build(self) -> bool:
        """
        Distinguishes between build types that are potentially used in production releases from
        build types that are only used in testing (e.g. ASAN+UBSAN, TSAN).
        """
        return self.build_type in [BUILD_TYPE_COMMON, BUILD_TYPE_UNINSTRUMENTED]

    def cmake_build_type_for_test_only_dependencies(self) -> str:
        return 'Release' if self.is_release_build() else 'Debug'

    def check_cxx_compiler_flag(self, flag: str) -> bool:
        compiler_path = self.compiler_choice.get_cxx_compiler()
        log(f"Checking if the compiler {compiler_path} accepts the flag {flag}")
        process = subprocess.Popen(
            [compiler_path, '-x', 'c++', flag, '-'],
            stdin=subprocess.PIPE)
        assert process.stdin is not None
        process.stdin.write("int main() { return 0; }".encode('utf-8'))
        process.stdin.close()
        return process.wait() == 0

    def add_checked_flag(self, flags: List[str], flag: str) -> None:
        if self.check_cxx_compiler_flag(flag):
            flags.append(flag)

    def get_openssl_dir(self) -> str:
        return os.path.join(self.fs_layout.tp_installed_common_dir)

    def get_openssl_related_cmake_args(self) -> List[str]:
        """
        Returns a list of CMake arguments to use to pick up the version of OpenSSL that we should be
        using. Returns an empty list if the default OpenSSL installation should be used.
        """
        openssl_dir = self.get_openssl_dir()
        openssl_options = ['-DOPENSSL_ROOT_DIR=' + openssl_dir]
        openssl_crypto_library = os.path.join(openssl_dir, 'lib', 'libcrypto.' + self.dylib_suffix)
        openssl_ssl_library = os.path.join(openssl_dir, 'lib', 'libssl.' + self.dylib_suffix)
        openssl_options += [
            '-DOPENSSL_CRYPTO_LIBRARY=' + openssl_crypto_library,
            '-DOPENSSL_SSL_LIBRARY=' + openssl_ssl_library,
            '-DOPENSSL_LIBRARIES=%s;%s' % (openssl_crypto_library, openssl_ssl_library)
        ]
        return openssl_options
