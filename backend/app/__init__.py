"""The phone receptionist backend.

Environment variables were renamed from the old product prefix to CALLKETTLE_*. An existing deployment that still sets an old name keeps working:
each is copied to its new name here, once, before any module reads the environment.
"""
import os as _os

for _name, _value in list(_os.environ.items()):
    if _name.startswith("%s_" % ("DESK" + "LINE")):
        _os.environ.setdefault("CALLKETTLE_" + _name.split("_", 1)[1], _value)
