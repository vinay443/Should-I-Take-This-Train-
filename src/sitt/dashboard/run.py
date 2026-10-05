"""`sitt-dashboard`: start the Streamlit app with one command."""

import sys
from pathlib import Path

from streamlit.web import cli

APP = Path(__file__).with_name("app.py")


def main() -> None:
    sys.argv = ["streamlit", "run", str(APP), *sys.argv[1:]]
    sys.exit(cli.main())


if __name__ == "__main__":
    main()
