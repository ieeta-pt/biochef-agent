if [ ! -d "venv" ]; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || {
        echo "This needs Python 3.12 or newer; found $(python3 --version 2>&1)." >&2
        exit 1
    }
    python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt
fastapi run main.py