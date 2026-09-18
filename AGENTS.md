# Agent Instructions

## Secret Handling

- Do not read `.env`, `.env.*`, or any file that appears to contain secrets, credentials, API keys, tokens, passwords, private keys, or production configuration unless the user explicitly asks you to read that specific file in the current conversation.
- Use `.env.example` for environment variable names, expected configuration shape, and placeholder values.
- If a task seems to require a real secret value, stop and ask the user for permission before inspecting any secret-bearing file.
- Do not print, summarize, copy, transform, or store secret values.
- Do not suggest committing secret-bearing files. Keep `.env` and other local secret files out of version control.

## Commenting Style

- Prefer beginner-friendly comments and docstrings when adding or changing code.
- Explain the purpose of functions, classes, and non-obvious blocks in plain language.
- Lean slightly more commented than usual, as if the reader has limited technical experience.
- Focus comments on why something exists, what data shape is expected, and how pieces fit together.
- Avoid comments that only repeat the exact code behavior when the line is already obvious.
- For scripts and command-line flows, comment the major steps so a newer reader can follow the path from input to output.
