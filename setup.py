import sys
from setuptools import setup, find_packages, Extension

DECODERS = "external/espargos-packet-decoders"

setup(
    name="pyespargos",
    version="0.1.1",
    author="Florian Euchner",
    author_email="jeija@jeija.net",
    description="Python library for working with the ESPARGOS WiFi channel sounder",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/ESPARGOS/pyespargos",
    packages=find_packages() + ["espargos.packet_decoders"],
    package_dir={"espargos.packet_decoders": DECODERS + "/python/espargos_packet_decoders"},
    ext_modules=[
        Extension(
            "espargos.packet_decoders._native",
            sources=[DECODERS + "/" + source for source in ("bindings/python.c", "bindings/bridge.c", "src/wifi.c", "src/ble.c")],
            include_dirs=[DECODERS + "/include", DECODERS + "/bindings"],
            libraries=[] if sys.platform == "win32" else ["m"],
            extra_compile_args=["/std:c11"] if sys.platform == "win32" else ["-std=c99"],
        )
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.11",
    install_requires=["websockets>=12.0", "numpy>=1.26.0", "pyserial>=3.5", "aiohttp>=3.9.0"],
    include_package_data=True,
    license_files=["LICENSE", DECODERS + "/COPYING", DECODERS + "/COPYING.LESSER"],
)
