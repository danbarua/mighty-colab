"""Sibling module, imported by `train.py`.

Its only job is to prove that bundle imports resolve on the VM the same way
they do locally -- the failure mode this guards against is a script that
runs fine on a laptop and dies on its first `import model` remotely.
"""


def build_model():
    return {"layers": 2, "units": 16}
