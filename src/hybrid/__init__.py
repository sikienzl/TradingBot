"""
Hybrid Trading Architecture: Hailo-8 Edge + GPT-5 Cloud

This package implements the hybrid filtering strategy:
1. Hailo-8 (Node 2): High-frequency anomaly detection (26 TOPS, 100ms loops)
2. GPT-5 (Cloud): Called ONLY when anomaly detected (95% cost reduction)

Components:
  - hailo/: Hailo-8 edge inference & WebSocket listener
  - cloud/: GPT-5 chief strategist & decision gating
  - hybrid/: Orchestration & hybrid decision logic
"""

__version__ = "1.0.0-hybrid"
