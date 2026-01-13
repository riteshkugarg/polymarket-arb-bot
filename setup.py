from setuptools import setup, find_packages

setup(
    name="polymarket-arb-bot",
    version="1.0.0",
    description="Production-grade Polymarket arbitrage bot",
    author="Polymarket Team",
    python_requires=">=3.10",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        line.strip()
        for line in open("requirements.txt")
        if line.strip() and not line.startswith("#")
    ],
    extras_require={
        "dev": [
            line.strip()
            for line in open("requirements-dev.txt")
            if line.strip() and not line.startswith(("#", "-r"))
        ],
    },
    entry_points={
        "console_scripts": [
            "polymarket-bot=main:main",
        ],
    },
)
