#!/usr/bin/env python3
"""Generate a random token for API_TOKEN (or any secret).

    python gen_token.py          # one token
    python gen_token.py 3        # three tokens
    python gen_token.py 1 32     # one 32-byte token
"""
import secrets
import sys


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    nbytes = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    for _ in range(count):
        print(secrets.token_urlsafe(nbytes))


if __name__ == "__main__":
    main()
