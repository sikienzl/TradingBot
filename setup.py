from setuptools import setup, find_packages

# Read the contents of requirements.txt
with open('requirements.txt', 'r') as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith('#')]

setup(
    name="trading-bot",
    version="0.1.0",
    author="Trading Bot Developer",
    description="A modular cryptocurrency trading bot",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/sikienzl/TradingBot",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "trading-bot=src.main:main",
        ],
    },
    include_package_data=True,
)