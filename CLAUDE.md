# wyred (engine) — boundary rules

- Pure Python stdlib + wyred-contract schemas. **NEVER import wyred-harness** (or any checker): the engine is independent of its checkers; they meet only at wyred-contract artifacts on disk. This is the trust boundary that made ga019's C=5 possible — do not soften it.
- Port source: /Users/owebeeone/limbo/elecscad/rl/ga019/modeller/ . Port acceptance: re-emit **byte-identical** goldens (wyred-contract/goldens/ga019/).
- Contract changes are NEVER made here: propose in wyred-wz/dev-docs/, land in wyred-contract (single-writer).
- Plans/designs go to wyred-wz/dev-docs/, not this repo.
