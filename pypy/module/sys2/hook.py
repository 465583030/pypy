
def displayhook(space, w_obj):
    if not space.is_w(w_obj, space.w_None): 
        space.setitem(space.builtin.w_dict, space.wrap('_'), w_obj)
        # NB. this is slightly more complicated in CPython,
        # see e.g. the difference with  >>> print 5,; 8
        space.appexec([w_obj], """
            (obj): 
                print `obj` 
        """)

__displayhook__ = displayhook  # this is exactly like in CPython

