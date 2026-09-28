"""Send Ctrl+C only to a console containing the requested model and this helper."""
import ctypes
import os
import sys
import time


def main(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):return 2
    try:
        if not kernel.SetConsoleCtrlHandler(None,True):return 3
        members=(ctypes.c_ulong*256)()
        count=kernel.GetConsoleProcessList(members,len(members))
        if not count or count>len(members):return 4
        if set(members[:count])!={pid,os.getpid()}:return 5
        if not kernel.GenerateConsoleCtrlEvent(0,0):return 6
        time.sleep(.2)
        return 0
    finally:kernel.FreeConsole()


if __name__=='__main__':sys.exit(main(int(sys.argv[1])))
