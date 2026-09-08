NAME   := tribixbite/updl
TAG    := `git log -1 --pretty=%H`
IMG    := ${NAME}:${TAG}
LATEST := ${NAME}:latest

all: build

build:
	@docker build -t ${IMG} .
	@docker tag ${IMG} ${LATEST}

# Deliberately not part of 'all': pushing an image is an explicit decision.
push:
	@docker push ${NAME}
