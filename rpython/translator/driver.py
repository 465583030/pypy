import sys, os
import os.path
import shutil

from rpython.translator.translator import TranslationContext
from rpython.translator.goal import query
from rpython.translator.goal.timing import Timer
from rpython.annotator.listdef import s_list_of_strings
from rpython.annotator import policy as annpolicy
from rpython.tool.udir import udir
from rpython.rlib.debug import debug_start, debug_print, debug_stop
from rpython.rlib.entrypoint import secondary_entrypoints

import py
from rpython.tool.ansi_print import ansi_log
log = py.log.Producer("translation")
py.log.setconsumer("translation", ansi_log)


def taskdef(title, idemp=False, earlycheck=None):
    def decorator(taskfunc):
        assert taskfunc.__name__.startswith('task_')
        taskfunc.task_name = taskfunc.__name__[len('task_'):]
        taskfunc.task_title = title
        taskfunc.task_idempotent = idemp
        taskfunc.task_earlycheck = earlycheck
        return taskfunc
    return decorator


class CBackend(object):
    def __init__(self, driver):
        self.driver = driver

    def possibly_check_for_boehm(self):
        if self.config.translation.gc == "boehm":
            from rpython.rtyper.tool.rffi_platform import configure_boehm
            from rpython.translator.platform import CompilationError
            try:
                configure_boehm(self.translator.platform)
            except CompilationError, e:
                i = 'Boehm GC not installed.  Try e.g. "translate.py --gc=hybrid"'
                raise Exception(str(e) + '\n' + i)

    @taskdef("Creating database for generating c source",
             earlycheck=possibly_check_for_boehm)
    def task_database(self):
        """ Create a database for further backend generation
        """
        translator = self.driver.translator
        if translator.annotator is not None:
            translator.frozen = True

        if self.driver.standalone:
            from rpython.translator.c.genc import CStandaloneBuilder
            cbuilder = CStandaloneBuilder(translator, self.driver.entry_point,
                                          config=self.driver.config,
                      secondary_entrypoints=self.driver.secondary_entrypoints)
        else:
            from rpython.translator.c.dlltool import CLibraryBuilder
            functions = [(self.driver.entry_point, None)] + \
                        self.driver.secondary_entrypoints
            cbuilder = CLibraryBuilder(translator, self.driver.entry_point,
                                       functions=functions,
                                       name='libtesting',
                                       config=self.driver.config)
            cbuilder.modulename = self.driver.extmod_name
        database = cbuilder.build_database()
        self.driver.log.info("database for generating C source was created")
        self.cbuilder = self.driver.cbuilder = cbuilder
        self.database = database

    @taskdef("Generating c source")
    def task_source(self):
        """ Create C source files from the generated database
        """
        cbuilder = self.cbuilder
        database = self.database
        debug_def = self.driver._backend_extra_options.get('c_debug_defines', False)
        if self.driver.exe_name is not None:
            exe_name = self.driver.exe_name % self.driver.get_info()
        else:
            exe_name = None
        c_source_filename = cbuilder.generate_source(
                database, debug_defines=debug_def, exe_name=exe_name)
        self.driver.log.info("written: %s" % (c_source_filename,))
        if self.driver.config.translation.dump_static_data_info:
            from rpython.translator.tool.staticsizereport import dump_static_data_info
            targetdir = cbuilder.targetdir
            fname = dump_static_data_info(self.driver.log, database, targetdir)
            dstname = self.driver.compute_exe_name() + '.staticdata.info'
            shutil.copy(str(fname), str(dstname))
            self.driver.log.info('Static data info written to %s' % dstname)

    @taskdef("Compiling c source")
    def task_compile(self):
        """ Compile the generated C code using either makefile or
        translator/platform
        """
        cbuilder = self.cbuilder
        kwds = {}
        if self.driver.standalone and self.driver.exe_name is not None:
            kwds['exe_name'] = self.driver.compute_exe_name().basename
        cbuilder.compile(**kwds)

        if self.driver.standalone:
            self.driver.c_entryp = cbuilder.executable_name
            self.create_exe()
        else:
            self.driver.c_entryp = cbuilder.get_entry_point()

    def create_exe(self):
        """ Copy the compiled executable into translator/goal
        """
        if self.driver.exe_name is not None:
            exename = self.driver.c_entryp
            newexename = self.driver.compute_exe_name()
            if sys.platform == 'win32':
                newexename = newexename.new(ext='exe')
            shutil.copy(str(exename), str(newexename))
            if self.cbuilder.shared_library_name is not None:
                soname = self.cbuilder.shared_library_name
                newsoname = newexename.new(basename=soname.basename)
                shutil.copy(str(soname), str(newsoname))
                self.driver.log.info("copied: %s" % (newsoname,))
                if sys.platform == 'win32':
                    shutil.copyfile(str(soname.new(ext='lib')),
                                    str(newsoname.new(ext='lib')))
            self.driver.c_entryp = newexename
        self.driver.log.info('usession directory: %s' % (udir,))
        self.driver.log.info("created: %s" % (self.driver.c_entryp,))

    def get_tasks(self):
        yield self.task_database
        yield self.task_source
        yield self.task_compile

backends = {'c': CBackend}


# TODO:
# sanity-checks using states

# set of translation steps to profile
PROFILE = set([])

class Instrument(Exception):
    pass


class ProfInstrument(object):
    name = "profinstrument"
    def __init__(self, datafile, compiler):
        self.datafile = datafile
        self.compiler = compiler

    def first(self):
        return self.compiler._build()

    def probe(self, exe, args):
        env = os.environ.copy()
        env['PYPY_INSTRUMENT_COUNTERS'] = str(self.datafile)
        self.compiler.platform.execute(exe, args, env=env)

    def after(self):
        # xxx
        os._exit(0)


class TranslationDriver(object):
    _backend_extra_options = {}

    def __init__(self, setopts=None,
                 exe_name=None, extmod_name=None,
                 config=None, overrides=None):
        self.timer = Timer()

        self.log = log

        if config is None:
            from rpython.config.translationoption import get_combined_translation_config
            config = get_combined_translation_config(translating=True)
        self.config = config
        if overrides is not None:
            self.config.override(overrides)

        if setopts is not None:
            self.config.set(**setopts)

        self.exe_name = exe_name
        self.extmod_name = extmod_name

        self.done = {}

        self.tasks = tasks = []
        # expose tasks
        def expose_task(task):
            def proc():
                return self.proceed(task)
            tasks.append(task)
            setattr(self, task.task_name, proc)

        expose_task(self.task_annotate)
        expose_task(self.task_rtype)
        if config.translation.jit:
            expose_task(self.task_pyjitpl)
        if not config.translation.backendopt.none:
            expose_task(self.task_backendopt)
        if config.translation.type_system == 'lltype':
            expose_task(self.task_stackcheckinsertion_lltype)
        for task in backends[config.translation.backend](self).get_tasks():
            expose_task(task)

    def set_backend_extra_options(self, extra_options):
        self._backend_extra_options = extra_options

    def get_info(self): # XXX more?
        d = {'backend': self.config.translation.backend}
        return d

    def setup(self, entry_point, inputtypes, policy=None, extra={}, empty_translator=None):
        standalone = inputtypes is None
        self.standalone = standalone

        if standalone:
            # the 'argv' parameter
            inputtypes = [s_list_of_strings]
        self.inputtypes = inputtypes

        if policy is None:
            policy = annpolicy.AnnotatorPolicy()
        self.policy = policy

        self.extra = extra

        if empty_translator:
            translator = empty_translator
        else:
            translator = TranslationContext(config=self.config)

        self.entry_point = entry_point
        self.translator = translator
        self.libdef = None
        self.secondary_entrypoints = []

        if self.config.translation.secondaryentrypoints:
            for key in self.config.translation.secondaryentrypoints.split(","):
                try:
                    points = secondary_entrypoints[key]
                except KeyError:
                    raise KeyError(
                        "Entrypoints not found. I only know the keys %r." %
                        (", ".join(secondary_entrypoints.keys()), ))
                self.secondary_entrypoints.extend(points)

        self.translator.driver_instrument_result = self.instrument_result

    def setup_library(self, libdef, policy=None, extra={}, empty_translator=None):
        """ Used by carbon python only. """
        self.setup(None, None, policy, extra, empty_translator)
        self.libdef = libdef
        self.secondary_entrypoints = libdef.functions

    def instrument_result(self, args):
        backend = self.config.translation.backend
        if backend != 'c' or sys.platform == 'win32':
            raise Exception("instrumentation requires the c backend"
                            " and unix for now")

        datafile = udir.join('_instrument_counters')
        makeProfInstrument = lambda compiler: ProfInstrument(datafile, compiler)

        pid = os.fork()
        if pid == 0:
            # child compiling and running with instrumentation
            self.config.translation.instrument = True
            self.config.translation.instrumentctl = (makeProfInstrument,
                                                     args)
            raise Instrument
        else:
            pid, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                status = os.WEXITSTATUS(status)
                if status != 0:
                    raise Exception, "instrumentation child failed: %d" % status
            else:
                raise Exception, "instrumentation child aborted"
            import array, struct
            n = datafile.size()//struct.calcsize('L')
            datafile = datafile.open('rb')
            counters = array.array('L')
            counters.fromfile(datafile, n)
            datafile.close()
            return counters

    def info(self, msg):
        log.info(msg)

    def _profile(self, goal, func):
        from cProfile import Profile
        from rpython.tool.lsprofcalltree import KCacheGrind
        d = {'func':func}
        prof = Profile()
        prof.runctx("res = func()", globals(), d)
        KCacheGrind(prof).output(open(goal + ".out", "w"))
        return d['res']

    def _do(self, goal, func, *args, **kwds):
        title = func.task_title
        if goal in self.done:
            self.log.info("already done: %s" % title)
            return
        else:
            self.log.info("%s..." % title)
        debug_start('translation-task')
        debug_print('starting', goal)
        self.timer.start_event(goal)
        try:
            instrument = False
            try:
                if goal in PROFILE:
                    res = self._profile(goal, func)
                else:
                    res = func()
            except Instrument:
                instrument = True
            if not func.task_idempotent:
                self.done[goal] = True
            if instrument:
                xxx
                self.compile()
                assert False, 'we should not get here'
        finally:
            try:
                debug_stop('translation-task')
                self.timer.end_event(goal)
            except Exception:
                pass
        return res

    @taskdef("Annotating&simplifying")
    def task_annotate(self):
        """ Annotate
        """
        # includes annotation and annotatation simplifications
        translator = self.translator
        policy = self.policy
        self.log.info('with policy: %s.%s' % (policy.__class__.__module__, policy.__class__.__name__))

        annotator = translator.buildannotator(policy=policy)

        if self.secondary_entrypoints is not None:
            for func, inputtypes in self.secondary_entrypoints:
                if inputtypes == Ellipsis:
                    continue
                rettype = annotator.build_types(func, inputtypes, False)

        if self.entry_point:
            s = annotator.build_types(self.entry_point, self.inputtypes)
            translator.entry_point_graph = annotator.bookkeeper.getdesc(self.entry_point).getuniquegraph()
        else:
            s = None

        self.sanity_check_annotation()
        if self.entry_point and self.standalone and s.knowntype != int:
            raise Exception("stand-alone program entry point must return an "
                            "int (and not, e.g., None or always raise an "
                            "exception).")
        annotator.complete()
        annotator.simplify()
        return s

    def sanity_check_annotation(self):
        translator = self.translator
        irreg = query.qoutput(query.check_exceptblocks_qgen(translator))
        if irreg:
            self.log.info("Some exceptblocks seem insane")

        lost = query.qoutput(query.check_methods_qgen(translator))
        assert not lost, "lost methods, something gone wrong with the annotation of method defs"

    @taskdef("RTyping")
    def task_rtype(self):
        """ RTyping
        """
        type_system = self.config.translation.type_system
        rtyper = self.translator.buildrtyper(type_system)
        rtyper.specialize(dont_simplify_again=True)

    @taskdef("JIT compiler generation")
    def task_pyjitpl(self):
        """ Generate bytecodes for JIT and flow the JIT helper functions
        """
        get_policy = self.extra['jitpolicy']
        self.jitpolicy = get_policy(self)
        #
        from rpython.jit.metainterp.warmspot import apply_jit
        apply_jit(self.translator, policy=self.jitpolicy,
                  backend_name=self.config.translation.jit_backend, inline=True)
        #
        self.log.info("the JIT compiler was generated")

    @taskdef("test of the JIT on the llgraph backend")
    def task_jittest(self):
        """ Run with the JIT on top of the llgraph backend
        """
        # parent process loop: spawn a child, wait for the child to finish,
        # print a message, and restart
        from rpython.translator.goal import unixcheckpoint
        unixcheckpoint.restartable_point(auto='run')
        # load the module rpython/jit/tl/jittest.py, which you can hack at
        # and restart without needing to restart the whole translation process
        from rpython.jit.tl import jittest
        jittest.jittest(self)

    @taskdef("back-end optimisations")
    def task_backendopt(self):
        """ Run all backend optimizations
        """
        from rpython.translator.backendopt.all import backend_optimizations
        backend_optimizations(self.translator)

    @taskdef("inserting stack checks")
    def task_stackcheckinsertion_lltype(self):
        from rpython.translator.transform import insert_ll_stackcheck
        count = insert_ll_stackcheck(self.translator)
        self.log.info("inserted %d stack checks." % (count,))

    @taskdef("LLInterpreting")
    def task_llinterpret_lltype(self):
        from rpython.rtyper.llinterp import LLInterpreter
        py.log.setconsumer("llinterp operation", None)

        translator = self.translator
        interp = LLInterpreter(translator.rtyper)
        bk = translator.annotator.bookkeeper
        graph = bk.getdesc(self.entry_point).getuniquegraph()
        v = interp.eval_graph(graph,
                              self.extra.get('get_llinterp_args',
                                             lambda: [])())

        log.llinterpret.event("result -> %s" % v)

    def compute_exe_name(self):
        newexename = self.exe_name % self.get_info()
        if '/' not in newexename and '\\' not in newexename:
            newexename = './' + newexename
        return py.path.local(newexename)

    @taskdef('Generating CLI source')
    def task_source_cli(self):
        from rpython.translator.cli.gencli import GenCli
        from rpython.translator.cli.entrypoint import get_entrypoint

        if self.entry_point is not None: # executable mode
            entry_point_graph = self.translator.graphs[0]
            entry_point = get_entrypoint(entry_point_graph)
        else:
            # library mode
            assert self.libdef is not None
            bk = self.translator.annotator.bookkeeper
            entry_point = self.libdef.get_entrypoint(bk)

        self.gen = GenCli(udir, self.translator, entry_point, config=self.config)
        filename = self.gen.generate_source()
        self.log.info("Wrote %s" % (filename,))

    @taskdef('Compiling CLI source')
    def task_compile_cli(self):
        from rpython.translator.oosupport.support import unpatch_os
        from rpython.translator.cli.test.runtest import CliFunctionWrapper
        filename = self.gen.build_exe()
        self.c_entryp = CliFunctionWrapper(filename)
        # restore original os values
        if hasattr(self, 'old_cli_defs'):
            unpatch_os(self.old_cli_defs)

        self.log.info("Compiled %s" % filename)
        if self.standalone and self.exe_name:
            self.copy_cli_exe()

    def copy_cli_exe(self):
        # XXX messy
        main_exe = self.c_entryp._exe
        usession_path, main_exe_name = os.path.split(main_exe)
        pypylib_dll = os.path.join(usession_path, 'pypylib.dll')

        basename = self.exe_name % self.get_info()
        dirname = basename + '-data/'
        if '/' not in dirname and '\\' not in dirname:
            dirname = './' + dirname

        if not os.path.exists(dirname):
            os.makedirs(dirname)
        shutil.copy(main_exe, dirname)
        shutil.copy(pypylib_dll, dirname)
        if bool(os.getenv('PYPY_GENCLI_COPYIL')):
            shutil.copy(os.path.join(usession_path, 'main.il'), dirname)
        newexename = basename
        f = file(newexename, 'w')
        f.write(r"""#!/bin/bash
LEDIT=`type -p ledit`
EXE=`readlink $0`
if [ -z $EXE ]
then
    EXE=$0
fi
if  uname -s | grep -iq Cygwin
then 
    MONO=
else 
    MONO=mono
    # workaround for known mono buggy versions
    VER=`mono -V | head -1 | sed s/'Mono JIT compiler version \(.*\) (.*'/'\1/'`
    if [[ 2.1 < "$VER" && "$VER" < 2.4.3 ]]
    then
        MONO="mono -O=-branch"
    fi
fi
$LEDIT $MONO "$(dirname $EXE)/$(basename $EXE)-data/%s" "$@" # XXX doesn't work if it's placed in PATH
""" % main_exe_name)
        f.close()
        os.chmod(newexename, 0755)

    def copy_cli_dll(self):
        dllname = self.gen.outfile
        usession_path, dll_name = os.path.split(dllname)
        pypylib_dll = os.path.join(usession_path, 'pypylib.dll')
        shutil.copy(dllname, '.')
        shutil.copy(pypylib_dll, '.')

        # main.exe is a stub but is needed right now because it's
        # referenced by pypylib.dll.  Will be removed in the future
        translator_path, _ = os.path.split(__file__)
        main_exe = os.path.join(translator_path, 'cli/src/main.exe')
        shutil.copy(main_exe, '.')
        self.log.info("Copied to %s" % os.path.join(os.getcwd(), dllname))

    @taskdef('Generating JVM source')
    def task_source_jvm(self):
        from rpython.translator.jvm.genjvm import GenJvm
        from rpython.translator.jvm.node import EntryPoint

        entry_point_graph = self.translator.graphs[0]
        is_func = not self.standalone
        entry_point = EntryPoint(entry_point_graph, is_func, is_func)
        self.gen = GenJvm(udir, self.translator, entry_point)
        self.jvmsource = self.gen.generate_source()
        self.log.info("Wrote JVM code")

    @taskdef('Compiling JVM source')
    def task_compile_jvm(self):
        from rpython.translator.oosupport.support import unpatch_os
        from rpython.translator.jvm.test.runtest import JvmGeneratedSourceWrapper
        self.jvmsource.compile()
        self.c_entryp = JvmGeneratedSourceWrapper(self.jvmsource)
        # restore original os values
        if hasattr(self, 'old_cli_defs'):
            unpatch_os(self.old_cli_defs)
        self.log.info("Compiled JVM source")
        if self.standalone and self.exe_name:
            self.copy_jvm_jar()

    def copy_jvm_jar(self):
        import subprocess
        basename = self.exe_name % self.get_info()
        root = udir.join('pypy')
        manifest = self.create_manifest(root)
        jnajar = py.path.local(__file__).dirpath('jvm', 'src', 'jna.jar')
        classlist = self.create_classlist(root, [jnajar])
        jarfile = py.path.local(basename + '.jar')
        self.log.info('Creating jar file')
        oldpath = root.chdir()
        subprocess.call(['jar', 'cmf', str(manifest), str(jarfile), '@'+str(classlist)])
        oldpath.chdir()

        # create a convenience script
        newexename = basename
        f = file(newexename, 'w')
        f.write("""#!/bin/bash
LEDIT=`type -p ledit`
EXE=`readlink $0`
if [ -z $EXE ]
then
    EXE=$0
fi
$LEDIT java -Xmx256m -jar $EXE.jar "$@"
""")
        f.close()
        os.chmod(newexename, 0755)

    def create_manifest(self, root):
        filename = root.join('manifest.txt')
        manifest = filename.open('w')
        manifest.write('Main-class: pypy.Main\n\n')
        manifest.close()
        return filename

    def create_classlist(self, root, additional_jars=[]):
        import subprocess
        # first, uncompress additional jars
        for jarfile in additional_jars:
            oldpwd = root.chdir()
            subprocess.call(['jar', 'xf', str(jarfile)])
            oldpwd.chdir()
        filename = root.join('classlist.txt')
        classlist = filename.open('w')
        classfiles = list(root.visit('*.class', True))
        classfiles += root.visit('*.so', True)
        classfiles += root.visit('*.dll', True)
        classfiles += root.visit('*.jnilib', True)
        for classfile in classfiles:
            print >> classlist, classfile.relto(root)
        classlist.close()
        return filename

    @taskdef('XXX')
    def task_run_jvm(self):
        pass

    def proceed(self, goal):
        tasks = []
        for task in self.tasks:
            tasks.append(task)
            if task == goal:
                break
        else:
            raise Exception("No such goal %s." % goal.task_name)

        for task in tasks:
            if task.task_earlycheck:
                task.task_earlycheck(self)
        res = None
        for task in tasks:
            res = self._do(task.task_name, task)
        return res

    def prereq_checkpt_rtype(self):
        assert 'rpython.rtyper.rmodel' not in sys.modules, (
            "cannot fork because the rtyper has already been imported")
