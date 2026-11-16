import sys

if __name__ == "__main__":
    if "--mcp" in sys.argv:          # Claude Desktop starts the app binary like this (no GUI, no Qt)
        from chartlab.mcp_server import serve
        serve()
    elif "--version" in sys.argv:
        from chartlab import __version__
        print(__version__)
    elif "--selftest" in sys.argv:   # the Mac build runs this: the real window, without a screen
        from chartlab.mainwindow import selftest
        sys.exit(selftest())
    else:
        from chartlab.mainwindow import main
        main()
