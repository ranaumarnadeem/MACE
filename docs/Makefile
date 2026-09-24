# Builds the MACE documentation into _build/. CI passes SPHINXOPTS="-W --keep-going".
SPHINXOPTS ?=

all: sphinx

sphinx:
	sphinx-build $(SPHINXOPTS) . _build

clean:
	rm -rf _build

.PHONY: all sphinx clean
