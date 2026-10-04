import sys


def main():
    if "--run-server" in sys.argv:
        from .launcher import run_server

        run_server()
    elif "--setup" in sys.argv:
        from .setup import main as setup

        setup()
    else:
        from .desktop import main as desktop

        desktop()


if __name__ == "__main__":
    main()
