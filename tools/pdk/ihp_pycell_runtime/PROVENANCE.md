# `ihp_pycell_runtime` — vendored, and why

Both ihp families in the image ship a PCell library that cannot load
(vibeic-eda#173). `libs.tech/klayout/tech/pymacros/autorun.lym` — the PDK's own
bootstrap, run by KLayout for the `sg13g2` / `sg13cmos5l` technologies — does

```python
append_if_exists(os.path.join(base_dir, '..', '..', 'python'))
append_if_exists(os.path.join(base_dir, '..', '..', 'python', 'pycell4klayout-api', 'source', 'python'))
```

and `python/sg13g2_pycell_lib/__init__.py` then does

```python
from cni.tech import Tech                                              # :19
from cni.dlo import PCellWrapper                                       # :20
from pypreprocessor.pypreprocessor import preprocessor as preProcessor # :27
```

In the shipped volume `python/pycell4klayout-api/` and `python/pypreprocessor/`
are **empty directories**: upstream carries both as git submodules, and the
prebuilt PDK was cut from a checkout that never initialised them. So
`ModuleNotFoundError: No module named 'cni'`, `SG13_dev` never registers, and
every consumer of the library — the seal-ring script among them — fails.

`ihp-sg13cmos5l` is the same gap, not a second one: in the image and in its own
repository (`IHP-GmbH/ihp-sg13cmos5l` @ `9f614c48`), its two entries are
symlinks `../../../../ihp-sg13g2/libs.tech/klayout/python/<name>`. Filling the
sg13g2 directories fills both families.

## Provenance, established by content and not by assertion

The shipped sg13g2 tree records its upstream commit in `/foss/pdks/ihp-sg13g2/COMMIT`:

    IHP-GmbH/IHP-Open-PDK  5e6d592e4002946a4616f798c357f0f3c06cf3b6

That the shipped tree IS that commit, and not merely labelled with it, was
measured: the two consumers above are byte-identical to upstream at that sha —

    b0098e39f291a1ae1c8b398f0062c260b001960cac146912d93af28d591e48c9  libs.tech/klayout/tech/pymacros/autorun.lym
    9f86d604aab4cab639740c2b92bac9192f36ebcdc6d0b0a827fefe88ae2be137  libs.tech/klayout/python/sg13g2_pycell_lib/__init__.py

(sha256 of the file inside `ghcr.io/vibeic/vibeic-eda:0.3.46` on the left; the
raw file at the commit gives the same digest.) At that commit the tree carries
two gitlinks — read from the GitHub tree API, `type: submodule`:

    ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api  21c055516645d2f8f00335d81e92084e88b3c7b0  https://github.com/IHP-GmbH/pycell4klayout-api
    ihp-sg13g2/libs.tech/klayout/python/pypreprocessor      cf1ff9bad0fb5338cf1c5b990b2b816b1ea01a64  https://github.com/IHP-GmbH/pypreprocessor

This directory is those two gitlinks, restricted to what the PDK loads:

| vendored | from | what |
|---|---|---|
| `pycell4klayout-api/source/python/cni/*.py` (34) | pycell4klayout-api @ 21c05551 | the `cni` package (a namespace package; no `__init__.py` upstream either) |
| `pycell4klayout-api/LICENSE` | same | GPL-3.0 |
| `pypreprocessor/pypreprocessor/__init__.py` | pypreprocessor @ cf1ff9ba | the `pypreprocessor.pypreprocessor` module |
| `pypreprocessor/LICENSE` | same | MIT |

Not vendored: `doc/pycell4klayout.pdf` (387 KB, nothing imports it), `.pylintrc`,
`setup.py`, `Examples/`, `tests/`, `.DS_Store` — none is on the path the PDK
builds.

`MANIFEST.sha256` lists every vendored file as `<sha256>  <git blob id>  <path>`.
The blob id is what makes this checkable without trust: every one of the 37
equals the blob id the upstream tree reports for that path at that gitlink
commit (measured 2026-09-07, 37/37). `test_pdk_ihp_pycell_runtime.py` recomputes
the sha256 column on every run, so the tree cannot drift from this record.

## Licences

`cni` is GPL-3.0; `pypreprocessor` is MIT. Each is kept whole under its own
`LICENSE` and is not relicensed by this repository. The image already
distributes GPL-3.0 software (KLayout itself); this adds a library that KLayout
loads, under the same terms.

## What this does NOT claim

* It does not claim the PCell library is correct — only that it now imports and
  registers the libraries its own source names (`SG13_dev`,
  `SG13_native_pcell_lib` for sg13g2). `pdk_pycell_lib_check.py` measures that.
* It does not make ihp-sg13cmos5l whole: two of that library's device modules are
  dangling symlinks into a newer sg13g2 (vibeic-eda#176); `pdk_pycell_lib_check.py`
  reports that on every build, and the build gates only ihp-sg13g2 on it.
* It is not under the ciel digest root (`/foss/pdks/ciel`), because the ihp
  families are not delivered through ciel; `tools/pdk/local_mods.json` therefore
  carries no entry for it, exactly as for the OSDI objects built in the same
  Dockerfile. The install program's own counts are the tripwire instead.
