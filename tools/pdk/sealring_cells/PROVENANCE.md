# `sealring_cells` — vendored, and why

`gf180mcuD/libs.tech/klayout/tech/scripts/sealring.py` ships **inside the prebuilt
ciel PDK volume** of the base image. Its first act is

```python
sys.path.insert(1, os.path.join(os.getenv("PDK_ROOT"), os.getenv("PDK"),
                                "libs.tech", "klayout", "tech", "pymacros"))
from sealring_cells import gf180mcu_sealring
```

and that package is **not** in the volume, so the script exits with
`Error: Couldn't load the seal ring library.` and no seal ring can be generated.
Reported as vibeic-eda#170 after `General.SealRing` turned from NOT_DETERMINED
into FAIL "the die is unsealed".

## Provenance, established by content and not by assertion

Upstream: `fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pr`
commit `e11a8c98dfc905561d38813b1416e65a4b9280e9`, path
`cells/klayout/pymacros/sealring_cells/`.

The same commit also carries `cells/klayout/scripts/sealring.py`, and that file is
**byte-identical** to the copy the image already ships:

```
84cfcb8e2d5cacb018af11856fe8df07f77f10a5b27a476247d5e2599f800ece
  upstream cells/klayout/scripts/sealring.py           @ e11a8c98
  image    gf180mcuD/.../tech/scripts/sealring.py      (0.3.46, sha256:06537f7e…)
```

So these four files are not a plausible-looking substitute for the missing half —
they are the matching half of the exact commit whose other half the volume
already contains, and the sha256 above is what proves it.

Licence: Apache-2.0 (Copyright 2025 Leo Moser). Open PDK content only.
