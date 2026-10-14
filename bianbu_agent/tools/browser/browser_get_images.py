import json
from bianbu_agent.tools import tool
from .client import run

_IMAGE_SCRIPT = "JSON.stringify([...document.images].map(img => ({src: img.src, alt: img.alt || '', width: img.naturalWidth, height: img.naturalHeight})).filter(img => img.src && !img.src.startsWith('data:')))"


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_get_images():
        """Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first."""
        return json.dumps(
            await run(package_context.call.session_id, "eval", (_IMAGE_SCRIPT,)), ensure_ascii=False
        )

    return browser_get_images
