import sys
from screenstudy.ui import run

if __name__ == "__main__":
    raise SystemExit(run(demo="--demo" in sys.argv))
