#!/usr/bin/env python3
"""Legacy entry point retained for compatibility.

Automatic model selection was removed because it could download an arbitrary,
very large model without integrity verification. Use download_model.py with an
exact repository, filename, and SHA-256 digest instead.
"""
print("Automatic GGUF discovery/download is disabled for safety.")
print("Use: python download_model.py owner/repository exact-model.gguf --sha256 <digest>")
