"""Command-line pieces the cresetup steps share.

  SingleValue   an argparse action refusing a switch given twice: the setup tasks pass the
                project's own values from .projenrc.ts, so a second one would set up another
                project, repo or account quietly.
  key_value(s)  a CI/CD variable, KEY=VALUE or KEY=@<file> (the file's text, one trailing newline
                dropped), so a secret is never on the command line, in `ps` or the shell history.
"""
import argparse


class SingleValue(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, f"_given_{self.dest}", False):
            parser.error(f"{option_string} was given twice (the setup task sets it from .projenrc.ts)")
        setattr(namespace, f"_given_{self.dest}", True)
        setattr(namespace, self.dest, values)


def key_value(s, refused_prefix=""):
    k, sep, v = s.partition("=")
    if not sep or not k or not k.replace("_", "").isalnum() or (refused_prefix and k.upper().startswith(refused_prefix)):
        not_prefix = f"; not {refused_prefix}*" if refused_prefix else ""
        # The key only: the value may be a secret.
        raise argparse.ArgumentTypeError(f"{k!r}: expected KEY=VALUE or KEY=@<file> (a key of letters, digits, _{not_prefix})")
    if v.startswith("@"):
        try:
            with open(v[1:]) as f:
                v = f.read()
        except OSError as e:
            raise argparse.ArgumentTypeError(f"{k}: {v[1:]} can't be read ({e.strerror})")
        v = v[:-1] if v.endswith("\n") else v
    return k, v
