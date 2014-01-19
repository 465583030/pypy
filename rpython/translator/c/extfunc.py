import types
from rpython.annotator import model as annmodel
from rpython.flowspace.model import FunctionGraph
from rpython.rtyper.lltypesystem import lltype
from rpython.translator.c.support import cdecl
from rpython.rtyper.lltypesystem.rstr import STR, mallocstr
from rpython.rtyper.lltypesystem import rstr
from rpython.rtyper.lltypesystem import rlist

# table of functions hand-written in src/ll_*.h
# Note about *.im_func: The annotator and the rtyper expect direct
# references to functions, so we cannot insert classmethods here.

EXTERNALS = {'LL_flush_icache': 'LL_flush_icache'}

#______________________________________________________

def predeclare_common_types(db, rtyper):
    # Common types
    yield ('RPyString', STR)

def predeclare_extfuncs(db, rtyper):
    modules = {}
    def module_name(c_name):
        frags = c_name[3:].split('_')
        if frags[0] == '':
            return '_' + frags[1]
        else:
            return frags[0]

    for func, funcobj in db.externalfuncs.items():
        c_name = EXTERNALS[func]
        # construct a define LL_NEED_<modname> to make it possible to isolate in-development externals and headers
        modname = module_name(c_name)
        if modname not in modules:
            modules[modname] = True
            yield 'LL_NEED_%s' % modname.upper(), 1
        funcptr = funcobj._as_ptr()
        yield c_name, funcptr

def predeclare_exception_data(db, rtyper):
    # Exception-related types and constants
    exceptiondata = rtyper.exceptiondata
    exctransformer = db.exctransformer

    yield ('RPYTHON_EXCEPTION',        exceptiondata.lltype_of_exception_value)
    yield ('RPYTHON_TYPE_OF_EXC_INST', exceptiondata.fn_type_of_exc_inst)

    yield ('RPyExceptionOccurred1',    exctransformer.rpyexc_occured_ptr.value)
    yield ('RPyFetchExceptionType',    exctransformer.rpyexc_fetch_type_ptr.value)
    yield ('RPyFetchExceptionValue',   exctransformer.rpyexc_fetch_value_ptr.value)
    yield ('RPyClearException',        exctransformer.rpyexc_clear_ptr.value)
    yield ('RPyRaiseException',        exctransformer.rpyexc_raise_ptr.value)

    for exccls in exceptiondata.standardexceptions:
        exc_llvalue = exceptiondata.get_standard_ll_exc_instance_by_class(
            exccls)
        rtyper.getrepr(annmodel.lltype_to_annotation(lltype.typeOf(exc_llvalue)))
        # strange naming here because the macro name must be
        # a substring of PyExc_%s
        name = exccls.__name__
        if exccls.__module__ != 'exceptions':
            name = '%s_%s' % (exccls.__module__.replace('.', '__'), name)
        yield ('RPyExc_%s' % name, exc_llvalue)
    rtyper.call_all_setups()


def predeclare_all(db, rtyper):
    for fn in [predeclare_common_types,
               predeclare_exception_data,
               predeclare_extfuncs,
               ]:
        for t in fn(db, rtyper):
            yield t


def get_all(db, rtyper):
    for fn in [predeclare_common_types,
               predeclare_exception_data,
               predeclare_extfuncs,
               ]:
        for t in fn(db, rtyper):
            yield t[1]

# ____________________________________________________________

def do_the_getting(db, rtyper):

    decls = list(get_all(db, rtyper))
    rtyper.specialize_more_blocks()

    for obj in decls:
        if isinstance(obj, lltype.LowLevelType):
            db.gettype(obj)
        elif isinstance(obj, FunctionGraph):
            db.get(rtyper.getcallable(obj))
        else:
            db.get(obj)


def pre_include_code_lines(db, rtyper):
    # generate some #defines that go before the #include to provide
    # predeclared well-known names for constant objects, functions and
    # types.  These names are then used by the #included files, like
    # g_exception.h.

    def predeclare(c_name, lowlevelobj):
        llname = db.get(lowlevelobj)
        assert '\n' not in llname
        return '#define\t%s\t%s' % (c_name, llname)

    def predeclaretype(c_typename, lowleveltype):
        typename = db.gettype(lowleveltype)
        return 'typedef %s;' % cdecl(typename, c_typename)

    decls = list(predeclare_all(db, rtyper))

    for c_name, obj in decls:
        if isinstance(obj, lltype.LowLevelType):
            yield predeclaretype(c_name, obj)
        elif isinstance(obj, FunctionGraph):
            yield predeclare(c_name, rtyper.getcallable(obj))
        else:
            yield predeclare(c_name, obj)
