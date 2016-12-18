from pypy.module.cpyext.api import h


freefunc = h.definitions['freefunc']
destructor = h.definitions['destructor']
printfunc = h.definitions['printfunc']
getattrfunc = h.definitions['getattrfunc']
getattrofunc = h.definitions['getattrofunc']
setattrfunc = h.definitions['setattrfunc']
setattrofunc = h.definitions['setattrofunc']
cmpfunc = h.definitions['cmpfunc']
reprfunc = h.definitions['reprfunc']
hashfunc = h.definitions['hashfunc']
richcmpfunc = h.definitions['richcmpfunc']
getiterfunc = h.definitions['getiterfunc']
iternextfunc = h.definitions['iternextfunc']
descrgetfunc = h.definitions['descrgetfunc']
descrsetfunc = h.definitions['descrsetfunc']
initproc = h.definitions['initproc']
newfunc = h.definitions['newfunc']
allocfunc = h.definitions['allocfunc']

unaryfunc = h.definitions['unaryfunc']
binaryfunc = h.definitions['binaryfunc']
ternaryfunc = h.definitions['ternaryfunc']
inquiry = h.definitions['inquiry']
lenfunc = h.definitions['lenfunc']
coercion = h.definitions['coercion']
intargfunc = h.definitions['intargfunc']
intintargfunc = h.definitions['intintargfunc']
ssizeargfunc = h.definitions['ssizeargfunc']
ssizessizeargfunc = h.definitions['ssizessizeargfunc']
intobjargproc = h.definitions['intobjargproc']
intintobjargproc = h.definitions['intintobjargproc']
ssizeobjargproc = h.definitions['ssizeobjargproc']
ssizessizeobjargproc = h.definitions['ssizessizeobjargproc']
objobjargproc = h.definitions['objobjargproc']

objobjproc = h.definitions['objobjproc']
visitproc = h.definitions['visitproc']
traverseproc = h.definitions['traverseproc']

getter = h.definitions['getter']
setter = h.definitions['setter']

#wrapperfunc = h.definitions['wrapperfunc']
#wrapperfunc_kwds = h.definitions['wrapperfunc_kwds']

readbufferproc = h.definitions['readbufferproc']
writebufferproc = h.definitions['writebufferproc']
segcountproc = h.definitions['segcountproc']
charbufferproc = h.definitions['charbufferproc']
getbufferproc = h.definitions['getbufferproc']
releasebufferproc = h.definitions['releasebufferproc']


PyGetSetDef = h.definitions['PyGetSetDef'].OF
PyNumberMethods = h.definitions['PyNumberMethods'].OF
PySequenceMethods = h.definitions['PySequenceMethods'].OF
PyMappingMethods = h.definitions['PyMappingMethods'].OF
PyBufferProcs = h.definitions['PyBufferProcs'].OF
PyMemberDef = h.definitions['PyMemberDef'].OF
