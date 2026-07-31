# `pyespargos` Addons

Addons extend pyespargos with additional functionality: board capabilities,
application modalities, demos, scripts and tools.

To install an addon, clone it into this directory:

```bash
git clone <addon-repository-url> addons/<addon-name>
```

Addons are loaded automatically on `import espargos`: every package matching
`addons/*/espargos_*/` is imported and performs its registration. Installed
packages declaring an `espargos.addons` entry point are loaded as well.
A failing addon is logged and skipped.

Everything in this directory except this README is ignored by git.
