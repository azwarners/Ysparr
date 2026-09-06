"""Replaceable upstream provider adapters."""

from ysparr.upstream.base import UpstreamAdapter, UpstreamError
from ysparr.upstream.http import OpenAIHTTPAdapter

__all__ = ["OpenAIHTTPAdapter", "UpstreamAdapter", "UpstreamError"]
