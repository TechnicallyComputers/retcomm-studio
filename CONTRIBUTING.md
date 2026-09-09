# Contributing

Thanks for looking. Issues and pull requests are welcome.

## Licensing of contributions — please read before opening a PR

This project is offered under two tracks: the [PolyForm Noncommercial
License](LICENSE) for noncommercial use, and a paid commercial license for
everyone else (see [COMMERCIAL.md](COMMERCIAL.md)).

That second track has a consequence for contributions. If a contribution
reached the project only under the repository's noncommercial license, the
maintainer would have no right to include it in a commercially licensed copy —
and a single such contribution, anywhere in the tree, would block commercial
licensing of the whole file it touched.

So, by submitting a contribution you confirm that:

1. You wrote it, or you otherwise have the right to submit it.
2. You grant the maintainer a perpetual, worldwide, irrevocable, royalty-free
   licence to use, modify, and sublicense your contribution **under any terms,
   including commercial terms**, alongside the rest of the project.
3. You retain your own copyright in what you wrote. This grants rights; it does
   not take them away, and it is not an assignment.

If you are contributing on behalf of an employer, make sure whoever owns your
work output is willing to grant the above before you submit.

If you would rather not grant that, please open an issue describing the change
instead of a pull request. A described bug is genuinely useful and carries none
of this baggage.

> This is the lightweight version. If the project starts taking contributions
> at any volume, it should move to a signed CLA reviewed by a lawyer rather
> than relying on this file.

## What does not need any of the above

* Bug reports, feature requests, and questions.
* Changes to files that are already carved out of the repository licence — see
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Those go upstream, to the
  project that owns them, not here.

## Practical notes

Before opening a PR, the non-GUI tests should pass. They need no GPU and no
game:

```bash
./build/json_null_test
./build/frames_load_test
./build/snes_load_test
./build/debug_client_test
python tests/snes_platform_test.py
```

CI runs these on Windows and Linux for every pull request.

New hooks belong in the runtime, not here — `tools/README.md` is the rule that
decides where a given piece of the debugging suite lives, and it is worth
reading before adding anything under `tools/`.
