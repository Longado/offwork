from setuptools import find_packages, setup


setup(
    name="offwork-capsule",
    version="0.2.0",
    description="A local CLI workbench for tasks, agent sessions, memory, and verified capsules.",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.9",
    entry_points={"console_scripts": ["offwork=offwork_capsule.cli:main"]},
)
