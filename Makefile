VERSION:=$(shell cat VERSION)-git.$(shell git rev-list --count HEAD)

.PHONY: clean phony

all: build

build: dist/fitbit
	cd dist;zip -r heedy-fitbit-plugin-${VERSION}.zip ./fitbit

clean:
	rm -rf dist



dist:
	mkdir dist

node_modules:
	npm i


dist/fitbit: node_modules
	mkdir -p dist/fitbit
	cp LICENSE ./dist/fitbit
	cp VERSION ./dist/fitbit
	cp requirements.txt ./dist/fitbit
	npm run build

debug: dist/fitbit
	npm run debug

