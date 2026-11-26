.PHONY: css

# Rebuilds the UI's stylesheet with the pinned Tailwind CLI. CI runs the same
# thing and fails if the committed file differs.
css:
	./ui/tailwind/build.sh
