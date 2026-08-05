# CrowdSight backend image
#
# SCAFFOLD ONLY. Fleshed out alongside Phase 1, Step 3.
#
# Python 3.12 is pinned exactly and deliberately: the CAMEL/OASIS dependency
# tree lags 3.13/3.14, and the reference host runs system Python 3.14. That
# mismatch is the reason the backend is containerised at all — do not relax the
# pin to match the host.

FROM python:3.12-slim

WORKDIR /app

# TODO(Step 3): copy backend/requirements.txt, pip install, copy source,
# create a non-root user, expose 5000, and set the entrypoint.

CMD ["python", "-c", "print('CrowdSight backend scaffold — not yet implemented')"]
