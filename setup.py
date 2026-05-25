from setuptools import find_packages, setup


setup(
    name="stockquan",
    version="0.1.0",
    description="Python stock quant dashboard with news/data ingestion and user-confirmed trading.",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.9",
    install_requires=[
        "pandas>=2.0",
        "numpy>=1.24",
        "requests>=2.31",
        "feedparser>=6.0",
        "yfinance>=0.2",
        "streamlit>=1.35",
        "plotly>=5.20",
        "python-dotenv>=1.0",
    ],
    extras_require={"dev": ["pytest>=8.0"]},
)
