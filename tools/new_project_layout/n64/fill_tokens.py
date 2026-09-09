#!/usr/bin/env python3
"""fill_tokens.py — @TOKEN@ substitution for the new-project templates.

    python3 fill_tokens.py templates/game.toml.in out/game.toml \
        NAME=Glover CARTID=NGVE ...

An UNKNOWN token left in the output is an ERROR, not a blank. A scaffolder that
silently writes `@SHA256@` into a contract produces a project whose ROM
verification can never succeed and whose failure surfaces much later, in the
launcher, as "not verified" with no clue why. Failing here names the token and
the file.

Values may also be supplied from a file of KEY=VALUE lines with --vars, which
is how setup_project.sh passes a probe result through without quoting a dozen
arguments per template.
"""

import argparse
import re
import sys

TOKEN = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def load_vars(pairs, files):
    out = {}
    for path in files or []:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise SystemExit(f"{path}: not KEY=VALUE: {line}")
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"not KEY=VALUE: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("template")
    ap.add_argument("output")
    ap.add_argument("pairs", nargs="*")
    ap.add_argument("--vars", action="append", help="file of KEY=VALUE lines")
    args = ap.parse_args()

    values = load_vars(args.pairs, args.vars)

    with open(args.template) as f:
        text = f.read()

    missing = sorted({m.group(1) for m in TOKEN.finditer(text)} - set(values))
    if missing:
        raise SystemExit(
            f"{args.template}: no value for {', '.join('@'+m+'@' for m in missing)}")

    text = TOKEN.sub(lambda m: values[m.group(1)], text)

    with open(args.output, "w") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
