"""Runtime package. Import submodules directly (``runtime.runtime``, ``runtime.regions`` …); no eager
imports here, so ``agents`` can use ``runtime.regions`` without an import cycle."""
