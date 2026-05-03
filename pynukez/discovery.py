"""
Discovery utilities for Nukez SDK.

Provides functions for API discovery, health checking, and pricing.
"""

import os
import httpx
from typing import Dict, Any
from .types import DiscoveryDoc, PriceInfo
from .errors import NukezError

_DEFAULT_BASE_URL = os.environ.get("NUKEZ_BASE_URL", "https://api.nukez.xyz")

def discover(base_url: str = _DEFAULT_BASE_URL, timeout: float = 10.0) -> DiscoveryDoc:
    """
    Discover Nukez API capabilities.
    
    Args:
        base_url: Nukez API base URL
        timeout: Request timeout in seconds
        
    Returns:
        DiscoveryDoc with API capabilities
        
    Note:
        Agents should call this first to understand available features.
    """
    base_url = base_url.rstrip('/')
    
    try:
        # Get discovery document [14]
        response = httpx.get(f"{base_url}/.well-known/nukez.json", timeout=timeout)
        response.raise_for_status()
        data = response.json()
        
        return DiscoveryDoc(
            api_version=data.get("api_version", "1.0"),
            service=data.get("service", "Nukez"),
            description=data.get("description", ""),
            auth_modes=data.get("auth_modes", ["signed_envelope"]),
            endpoints=data.get("endpoints", {}),
            features=data.get("features", []),
            status=data.get("status", "unknown")
        )
        
    except httpx.HTTPError as e:
        raise NukezError(f"Discovery failed: {e}")

def health_check(base_url: str = _DEFAULT_BASE_URL, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Check API health status [4].
    
    Args:
        base_url: Nukez API base URL  
        timeout: Request timeout in seconds
        
    Returns:
        Health status dict with 'healthy' bool and details
    """
    base_url = base_url.rstrip('/')
    
    try:
        # Use price endpoint as health check (public, no auth) [4]
        response = httpx.get(f"{base_url}/v1/price", timeout=timeout)
        
        if response.status_code == 200:
            data = response.json()
            return {
                "healthy": True,
                "latency_ms": response.elapsed.total_seconds() * 1000,
                "price_usd": data.get("price_usd"),
                "price_sol": data.get("price_sol"),
            }
        else:
            return {
                "healthy": False,
                "status_code": response.status_code,
                "error": response.text[:200]
            }
            
    except httpx.TimeoutException:
        return {"healthy": False, "error": "timeout"}
    except httpx.HTTPError as e:
        return {"healthy": False, "error": str(e)}

def get_current_price(base_url: str = _DEFAULT_BASE_URL, units: int = 1) -> PriceInfo:
    """
    Get current storage pricing [4].
    
    Args:
        base_url: Nukez API base URL
        units: Number of storage units
        
    Returns:
        PriceInfo with current pricing
        
    Note:
        This is a public endpoint - no authentication required.
    """
    base_url = base_url.rstrip('/')
    
    try:
        response = httpx.get(f"{base_url}/v1/price", params={"units": units})
        response.raise_for_status()
        data = response.json()
        
        return PriceInfo(
            units=units,
            unit_price_usd=data.get("unit_price_usd", 0.0),
            total_usd=data.get("total_usd", 0.0), 
            amount_sol=data.get("amount_sol", 0.0),
            amount_lamports=data.get("amount_lamports", 0),
            network=data.get("network", "devnet")
        )
        
    except httpx.HTTPError as e:
        raise NukezError(f"Failed to get pricing: {e}")
