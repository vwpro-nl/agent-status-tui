"""Keepalive: a fixed-interval, self-contained idle-silence pinger.

No sampling ladder, no controller, no boundary search, no good/bad
classification, no token/cache/quota/billing interpretation. Every provider
ping and activity check is implemented locally in this package (see
``providers.py``): one minimal, non-interactive, read-only CLI turn per
ping, and one filesystem mtime check per activity detection. This package
has no dependency on, and does not import, any other subsystem in this
project -- it would keep working unchanged if everything else were removed.
"""
