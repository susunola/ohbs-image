# Extension template

Copy this directory into its own repository, rename the package and implement
one extension kind. The included self-test is deterministic and network-free.

```sh
python -m build
pip install dist/*.whl
ohbs-image extension verify scanner example-scanner
```

Publish only after the certification command passes against every supported
core version in `integrations/compatibility-matrix.json`.
