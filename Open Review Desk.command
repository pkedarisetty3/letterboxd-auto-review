#!/bin/zsh
# Double-click this file in Finder. Keep its Terminal window open while writing.
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1

# Reuse the project's Python environment; set it up on first launch if needed.
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$PROJECT_DIR/.venv" || exit 1
fi
if ! "$PROJECT_DIR/.venv/bin/python" -c 'import openai, dotenv' >/dev/null 2>&1; then
  "$PROJECT_DIR/.venv/bin/python" -m pip install -r requirements.txt || exit 1
fi

# Source imports also work without a separate editable-package install.
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
"$PROJECT_DIR/.venv/bin/python" -m letterboxd_auto_review.web --host 0.0.0.0 --open
