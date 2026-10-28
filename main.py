import sys

if __name__ == "__main__":
    if "--mcp" in sys.argv:          # Claude Desktop starts the app binary like this (no GUI, no Qt)
        from chartlab.mcp_server import serve
        serve()
    else:
        from chartlab.mainwindow import main
        main()
