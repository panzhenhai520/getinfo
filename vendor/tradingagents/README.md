# TradingAgents embedded source

This directory contains the immutable official TradingAgents `v0.3.1` source
archive and its Apache-2.0 license. `PROVENANCE.json` records the exact tag
object, commit, reviewed SSH signature fingerprint and file hashes.

The normal `Dockerfile` installs the archive during image build with the fully
hashed `requirements-tradingagents.lock`. No TradingAgents source or Python
package is downloaded at container runtime, and none of the upstream CLI,
website, Ollama, Redis or database services is started.

## Offline application rebuild

First build and accept the dependency-bearing image on a connected build host,
then transfer it to the isolated host with `docker save` / `docker load`. With
the image locally available as `collectinfo:stage2-15`, rebuild the current
application layer without any network access:

```sh
docker build --no-cache --network=none -f Dockerfile.offline \
  -t collectinfo:stage2-15-offline .
```

The offline Dockerfile performs no package installation or source download. A
from-scratch rebuild on an empty host still requires the base/runtime OCI image
to be preloaded; that prerequisite is part of the acceptance evidence.

## License boundary

TradingAgents is Apache-2.0. Its required Backtrader dependency is
GPL-3.0-or-later, so distributing the combined runtime image requires a license
compliance review and preservation of the corresponding notices/source offer.
