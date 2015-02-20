##
# Copyright 2012-2014 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://vscentrum.be/nl/en),
# the Hercules foundation (http://www.herculesstichting.be/in_English)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# http://github.com/hpcugent/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Various test utility functions.

@author: Kenneth Hoste (Ghent University)
"""
import copy
import fileinput
import os
import re
import shutil
import sys
import tempfile
from unittest import TestCase
from vsc.utils import fancylogger
from vsc.utils.patterns import Singleton

import easybuild.tools.build_log as eb_build_log
import easybuild.tools.options as eboptions
import easybuild.tools.toolchain.utilities as tc_utils
import easybuild.tools.module_naming_scheme.toolchain as mns_toolchain
from easybuild.framework.easyconfig import easyconfig
from easybuild.framework.easyblock import EasyBlock
from easybuild.main import main
from easybuild.tools import config
from easybuild.tools.config import module_classes
from easybuild.tools.environment import modify_env
from easybuild.tools.filetools import mkdir, read_file
from easybuild.tools.module_naming_scheme import GENERAL_CLASS
from easybuild.tools.modules import modules_tool
from easybuild.tools.options import EasyBuildOptions


# make sure tests are robust against any non-default configuration settings;
# involves ignoring any existing configuration files that are picked up, and cleaning the environment
# this is tackled here rather than in suite.py, to make sure this is also done when test modules are ran separately

# keep track of any $EASYBUILD_TEST_X environment variables
test_env_var_prefix = 'EASYBUILD_TEST_'
eb_test_env_vars = dict([(key, val) for (key, val) in os.environ.items() if key.startswith(test_env_var_prefix)])
print "eb_test_env_vars: %s" % eb_test_env_vars

# clean up environment from unwanted $EASYBUILD_X env vars
for key in os.environ.keys():
    if key.startswith('EASYBUILD_'):
        print "Undefining $%s (value: %s)" % (key, os.environ[key])
        del os.environ[key]

# ignore any existing configuration files
go = EasyBuildOptions(go_useconfigfiles=False)
os.environ['EASYBUILD_IGNORECONFIGFILES'] = ','.join(go.options.configfiles)

# redefine $EASYBUILD_TEST_X env vars as $EASYBUILD_X
for testkey, val in eb_test_env_vars.items():
    key = 'EASYBUILD_%s' % testkey[len(test_env_var_prefix):]
    print "redefining $%s as $%s = '%s'" % (testkey, key, val)
    os.environ[key] = val


class EnhancedTestCase(TestCase):
    """Enhanced test case, provides extra functionality (e.g. an assertErrorRegex method)."""

    def assertErrorRegex(self, error, regex, call, *args, **kwargs):
        """Convenience method to match regex with the expected error message"""
        try:
            call(*args, **kwargs)
            str_kwargs = ', '.join(['='.join([k,str(v)]) for (k,v) in kwargs.items()])
            str_args = ', '.join(map(str, args) + [str_kwargs])
            self.assertTrue(False, "Expected errors with %s(%s) call should occur" % (call.__name__, str_args))
        except error, err:
            if hasattr(err, 'msg'):
                msg = err.msg
            elif hasattr(err, 'message'):
                msg = err.message
            elif hasattr(err, 'args'):  # KeyError in Python 2.4 only provides message via 'args' attribute
                msg = err.args[0]
            else:
                msg = err
            try:
                msg = str(msg)
            except UnicodeEncodeError:
                msg = msg.encode('utf8', 'replace')
            self.assertTrue(re.search(regex, msg), "Pattern '%s' is found in '%s'" % (regex, msg))
            self.assertTrue(re.search(regex, msg), "Pattern '%s' is found in '%s'" % (regex, msg))

    def setUp(self):
        """Set up testcase."""
        self.log = fancylogger.getLogger(self.__class__.__name__, fname=False)
        fd, self.logfile = tempfile.mkstemp(suffix='.log', prefix='eb-test-')
        os.close(fd)
        self.cwd = os.getcwd()
        self.test_prefix = tempfile.mkdtemp()

        # keep track of original environment to restore
        self.orig_environ = copy.deepcopy(os.environ)

        # keep track of original environment/Python search path to restore
        self.orig_sys_path = sys.path[:]

        self.orig_paths = {}
        for path in ['buildpath', 'installpath', 'sourcepath']:
            self.orig_paths[path] = os.environ.get('EASYBUILD_%s' % path.upper(), None)

        testdir = os.path.dirname(os.path.abspath(__file__))

        self.test_sourcepath = os.path.join(testdir, 'sandbox', 'sources')
        os.environ['EASYBUILD_SOURCEPATH'] = self.test_sourcepath
        os.environ['EASYBUILD_PREFIX'] = self.test_prefix
        self.test_buildpath = tempfile.mkdtemp()
        os.environ['EASYBUILD_BUILDPATH'] = self.test_buildpath
        self.test_installpath = tempfile.mkdtemp()
        os.environ['EASYBUILD_INSTALLPATH'] = self.test_installpath

        # make sure no deprecated behaviour is being triggered (unless intended by the test)
        # trip *all* log.deprecated statements by setting deprecation version ridiculously high
        self.orig_current_version = eb_build_log.CURRENT_VERSION
        os.environ['EASYBUILD_DEPRECATED'] = '10000000'

        init_config()

        # add test easyblocks to Python search path and (re)import and reload easybuild modules
        import easybuild
        sys.path.append(os.path.join(testdir, 'sandbox'))
        reload(easybuild)
        import easybuild.easyblocks
        reload(easybuild.easyblocks)
        import easybuild.easyblocks.generic
        reload(easybuild.easyblocks.generic)
        reload(easybuild.tools.module_naming_scheme)  # required to run options unit tests stand-alone

        modtool = modules_tool()
        self.reset_modulepath([os.path.join(testdir, 'modules')])
        # purge out any loaded modules with original $MODULEPATH before running each test
        modtool.purge()

    def tearDown(self):
        """Clean up after running testcase."""
        os.chdir(self.cwd)
        modify_env(os.environ, self.orig_environ)
        tempfile.tempdir = None

        # restore original Python search path
        sys.path = self.orig_sys_path

        # cleanup
        for path in [self.logfile, self.test_buildpath, self.test_installpath, self.test_prefix]:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError, err:
                pass

        for path in ['buildpath', 'installpath', 'sourcepath']:
            if self.orig_paths[path] is not None:
                os.environ['EASYBUILD_%s' % path.upper()] = self.orig_paths[path]
            else:
                if 'EASYBUILD_%s' % path.upper() in os.environ:
                    del os.environ['EASYBUILD_%s' % path.upper()]
        init_config()

    def reset_modulepath(self, modpaths):
        """Reset $MODULEPATH with specified paths."""
        modtool = modules_tool()
        for modpath in os.environ.get('MODULEPATH', '').split(os.pathsep):
            modtool.remove_module_path(modpath)
        # make very sure $MODULEPATH is totally empty
        # some paths may be left behind, e.g. when they contain environment variables
        # example: "module unuse Modules/$MODULE_VERSION/modulefiles" may not yield the desired result
        os.environ['MODULEPATH'] = ''
        for modpath in modpaths:
            modtool.add_module_path(modpath)

    def eb_main(self, args, do_build=False, return_error=False, logfile=None, verbose=False, raise_error=False):
        """Helper method to call EasyBuild main function."""
        cleanup()

        myerr = False
        if logfile is None:
            logfile = self.logfile
        # clear log file
        open(logfile, 'w').write('')

        try:
            main((args, logfile, do_build))
        except SystemExit:
            pass
        except Exception, err:
            myerr = err
            if verbose:
                print "err: %s" % err

        os.chdir(self.cwd)

        # make sure config is reinitialized
        init_config()

        if myerr and raise_error:
            raise myerr

        if return_error:
            return read_file(self.logfile), myerr
        else:
            return read_file(self.logfile)

    def setup_hierarchical_modules(self):
        """Setup hierarchical modules to run tests on."""
        mod_prefix = os.path.join(self.test_installpath, 'modules', 'all')

        # simply copy module files under 'Core' and 'Compiler' to test install path
        # EasyBuild is responsible for making sure that the toolchain can be loaded using the short module name
        mkdir(mod_prefix, parents=True)
        for mod_subdir in ['Core', 'Compiler', 'MPI']:
            src_mod_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'modules', mod_subdir)
            shutil.copytree(src_mod_path, os.path.join(mod_prefix, mod_subdir))

        # make sure only modules in a hierarchical scheme are available, mixing modules installed with
        # a flat scheme like EasyBuildMNS and a hierarhical one like HierarchicalMNS doesn't work
        self.reset_modulepath([mod_prefix, os.path.join(mod_prefix, 'Core')])

        # tweak use statements in modules to ensure correct paths
        mpi_pref = os.path.join(mod_prefix, 'MPI', 'GCC', '4.7.2', 'OpenMPI', '1.6.4')
        for modfile in [
            os.path.join(mod_prefix, 'Core', 'GCC', '4.7.2'),
            os.path.join(mod_prefix, 'Core', 'GCC', '4.8.3'),
            os.path.join(mod_prefix, 'Core', 'icc', '2013.5.192-GCC-4.8.3'),
            os.path.join(mod_prefix, 'Core', 'ifort', '2013.5.192-GCC-4.8.3'),
            os.path.join(mod_prefix, 'Compiler', 'GCC', '4.7.2', 'OpenMPI', '1.6.4'),
            os.path.join(mod_prefix, 'Compiler', 'intel', '2013.5.192-GCC-4.8.3', 'impi', '4.1.3.049'),
            os.path.join(mpi_pref, 'FFTW', '3.3.3'),
            os.path.join(mpi_pref, 'OpenBLAS', '0.2.6-LAPACK-3.4.2'),
            os.path.join(mpi_pref, 'ScaLAPACK', '2.0.2-OpenBLAS-0.2.6-LAPACK-3.4.2'),
        ]:
            for line in fileinput.input(modfile, inplace=1):
                line = re.sub(r"(module\s*use\s*)/tmp/modules/all",
                              r"\1%s/modules/all" % self.test_installpath,
                              line)
                sys.stdout.write(line)

    def setup_categorized_hmns_modules(self):
        """Setup categorized hierarchical modules to run tests on."""
        mod_prefix = os.path.join(self.test_installpath, 'modules', 'all')

        # simply copy module files under 'CategorizedHMNS/{Core,Compiler,MPI}' to test install path
        # EasyBuild is responsible for making sure that the toolchain can be loaded using the short module name
        mkdir(mod_prefix, parents=True)
        for mod_subdir in ['Core', 'Compiler', 'MPI']:
            src_mod_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'modules', 'CategorizedHMNS', mod_subdir)
            shutil.copytree(src_mod_path, os.path.join(mod_prefix, mod_subdir))
        # create empty module file directory to make C/Tcl modules happy
        mpi_pref = os.path.join(mod_prefix, 'MPI', 'GCC', '4.7.2', 'OpenMPI', '1.6.4')
        mkdir(os.path.join(mpi_pref, 'base'))

        # make sure only modules in the CategorizedHMNS are available
        self.reset_modulepath([os.path.join(mod_prefix, 'Core', 'compiler'),
                               os.path.join(mod_prefix, 'Core', 'toolchain')])

        # tweak use statements in modules to ensure correct paths
        for modfile in [
            os.path.join(mod_prefix, 'Core', 'compiler', 'GCC', '4.7.2'),
            os.path.join(mod_prefix, 'Compiler', 'GCC', '4.7.2', 'mpi', 'OpenMPI', '1.6.4'),
        ]:
            for line in fileinput.input(modfile, inplace=1):
                line = re.sub(r"(module\s*use\s*)/tmp/modules/all",
                              r"\1%s/modules/all" % self.test_installpath,
                              line)
                sys.stdout.write(line)


def cleanup():
    """Perform cleanup of singletons and caches."""
    # clear Singelton instances, to start afresh
    Singleton._instances.clear()

    # empty caches
    tc_utils._initial_toolchain_instances.clear()
    easyconfig._easyconfigs_cache.clear()
    easyconfig._easyconfig_files_cache.clear()
    mns_toolchain._toolchain_details_cache.clear()


def init_config(args=None, build_options=None):
    """(re)initialize configuration"""

    cleanup()

    # initialize configuration so config.get_modules_tool function works
    eb_go = eboptions.parse_options(args=args)
    config.init(eb_go.options, eb_go.get_options_by_section('config'))

    # initialize build options
    if build_options is None:
        build_options = {
            'valid_module_classes': module_classes(),
            'valid_stops': [x[0] for x in EasyBlock.get_steps()],
        }
    if 'suffix_modules_path' not in build_options:
        build_options.update({'suffix_modules_path': GENERAL_CLASS})
    config.init_build_options(build_options=build_options)

    return eb_go.options


def find_full_path(base_path, trim=(lambda x: x)):
    """
    Determine full path for given base path by looking in sys.path and PYTHONPATH.
    trim: a function that takes a path and returns a trimmed version of that path
    """

    full_path = None

    pythonpath = os.getenv('PYTHONPATH')
    if pythonpath:
        pythonpath = pythonpath.split(':')
    else:
        pythonpath = []
    for path in sys.path + pythonpath:
        tmp_path = os.path.join(trim(path), base_path)
        if os.path.exists(tmp_path):
            full_path = tmp_path
            break

    return full_path
