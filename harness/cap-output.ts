import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// A 4B model at 24k context fills its window by printing whole files; cap what each tool call returns.
const MAX_CHARS = 4000;

export default function (pi: ExtensionAPI) {
	pi.on("tool_result", async (event) => {
		const text = event.content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
		if (text.length <= MAX_CHARS) return;
		const head = text.slice(0, MAX_CHARS);
		const note = `\n[output cut at ${MAX_CHARS} of ${text.length} characters. Read the rest with the read tool (offset and limit) or grep -n PATTERN FILE | head -40]`;
		return { content: [{ type: "text", text: head + note }], structuredContent: undefined };
	});
}
