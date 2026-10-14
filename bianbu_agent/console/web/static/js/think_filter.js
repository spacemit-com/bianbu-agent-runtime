(function (root, factory) {
    const stripAssistantThinkBlocks = factory();
    root.stripAssistantThinkBlocks = stripAssistantThinkBlocks;
    if (typeof module === 'object' && module.exports) {
        module.exports = stripAssistantThinkBlocks;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    function stripAssistantThinkBlocks(value) {
        let text = value == null ? '' : String(value);
        let removed = false;

        text = text.replace(
            /<think\b[^>]*>[\s\S]*?<\/think\s*>/gi,
            function () {
                removed = true;
                return '';
            }
        );

        // Streaming responses can contain an opening tag long before the
        // closing tag arrives. Hide that unfinished block instead of flashing
        // private reasoning into the answer bubble.
        text = text.replace(
            /<think\b[^>]*>[\s\S]*$/i,
            function () {
                removed = true;
                return '';
            }
        );

        // Suppress a split opening tag such as "<thi" at the beginning of a
        // stream. Complete ordinary text elsewhere is left untouched.
        text = text.replace(
            /(^|[\r\n])[ \t]*<t(?:h(?:i(?:n(?:k)?)?)?)?$/i,
            function (_match, prefix) {
                removed = true;
                return prefix;
            }
        );

        text = text.replace(/<\/think\s*>/gi, function () {
            removed = true;
            return '';
        });

        if (removed) {
            text = text.replace(/^(?:[ \t]*\r?\n)+/, '');
        }
        return text;
    }

    return stripAssistantThinkBlocks;
}));
