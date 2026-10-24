/**
 * Schematic hierarchy tools: insert a hierarchical sheet, scaffold a sub-sheet,
 * list sheet instances and set a symbol's reference/unit per instance.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerSchematicHierarchyTools(server: McpServer, callKicadScript: Function) {
  // Link an existing sub-sheet into a parent
  server.tool(
    "add_hierarchical_sheet",
    "Insert a hierarchical-sheet reference block into a parent schematic, pointing at an existing sub-sheet file. Adds the sheet box, name/file fields, a sheet_instances path entry on the next page number, and fixes sub-sheet component instance paths so ERC resolves references.",
    {
      schematicPath: z.string().describe("Path to the parent .kicad_sch"),
      subsheetPath: z.string().describe("Path to the existing sub-sheet .kicad_sch to reference"),
      sheetName: z.string().optional().default("Sheet").describe("Display name for the sheet"),
      position: z
        .object({ x: z.number(), y: z.number() })
        .optional()
        .describe("Top-left of the sheet box in mm (default 50,50)"),
      size: z
        .object({ width: z.number(), height: z.number() })
        .optional()
        .describe("Sheet box size in mm (default 80x50)"),
    },
    async (args: any) => {
      const r = await callKicadScript("add_hierarchical_sheet", args);
      if (!r.success)
        return { content: [{ type: "text", text: `Failed: ${r.message || "Unknown error"}` }] };
      return {
        content: [
          {
            type: "text",
            text: `Added sheet '${r.sheet_name}' -> ${r.subsheet_path} (page ${r.page})`,
          },
        ],
      };
    },
  );

  // Remove a hierarchical sheet reference from a parent
  server.tool(
    "remove_hierarchical_sheet",
    "Remove a hierarchical-sheet reference from a parent schematic (the reverse of add_hierarchical_sheet). Identify the sheet by sheetName (matches the sheet's name property) or by subsheetPath (matched by basename against the sheet's file property). Deletes the (sheet ...) block and any matching (sheet_instances) page entry. Does NOT delete the sub-sheet .kicad_sch file on disk.",
    {
      schematicPath: z.string().describe("Path to the parent .kicad_sch"),
      sheetName: z
        .string()
        .optional()
        .describe("Sheet display name to remove (matches the Sheetname/Sheet name property)"),
      subsheetPath: z
        .string()
        .optional()
        .describe("Sub-sheet file to remove (matched by basename against the Sheetfile property)"),
    },
    async (args: any) => {
      const r = await callKicadScript("remove_hierarchical_sheet", args);
      if (!r.success)
        return { content: [{ type: "text", text: `Failed: ${r.message || "Unknown error"}` }] };
      return { content: [{ type: "text", text: r.message }] };
    },
  );

  // Set a custom property on a hierarchical sheet
  server.tool(
    "set_sheet_property",
    "Add or update a custom property on a hierarchical sheet's (sheet ...) block — e.g. cell identity or generator parameters carried as sheet metadata. Identify the sheet by sheetName or sheetPath (basename match against the sheet's file property). The property is created hidden if absent, otherwise its value is updated in place; the file's formatting is preserved. The built-in 'Sheet name'/'Sheet file' properties cannot be set here — use add/remove_hierarchical_sheet to manage the sheet link.",
    {
      schematicPath: z.string().describe("Path to the parent .kicad_sch"),
      sheetName: z
        .string()
        .optional()
        .describe("Sheet display name (matches the Sheetname/Sheet name property)"),
      sheetPath: z
        .string()
        .optional()
        .describe("Sub-sheet file (matched by basename against the Sheetfile property)"),
      key: z.string().describe("Property name (e.g. 'IS.Cell')"),
      value: z.string().describe("Property value"),
    },
    async (args: any) => {
      const r = await callKicadScript("set_sheet_property", args);
      if (!r.success)
        return { content: [{ type: "text", text: `Failed: ${r.message || "Unknown error"}` }] };
      return { content: [{ type: "text", text: r.message }] };
    },
  );

  // List hierarchical sheets and their properties
  server.tool(
    "get_sheet_properties",
    "List hierarchical sheets in a schematic with their name, file, uuid, position, and full property map (built-ins plus custom properties set via set_sheet_property). With sheetName or sheetPath, returns just that sheet.",
    {
      schematicPath: z.string().describe("Path to the parent .kicad_sch"),
      sheetName: z.string().optional().describe("Only this sheet (by display name)"),
      sheetPath: z.string().optional().describe("Only this sheet (by file basename)"),
    },
    async (args: any) => {
      const r = await callKicadScript("get_sheet_properties", args);
      if (!r.success)
        return { content: [{ type: "text", text: `Failed: ${r.message || "Unknown error"}` }] };
      return {
        content: [{ type: "text", text: JSON.stringify(r.sheets, null, 2) }],
      };
    },
  );

  // Create a sub-sheet file AND link it in one call
  server.tool(
    "create_hierarchical_subsheet",
    "Create a new sub-sheet .kicad_sch file and link it into a parent schematic in a single call (create_schematic + add_hierarchical_sheet). The fastest way to grow a hierarchical design.",
    {
      parentSchematicPath: z.string().describe("Path to the parent .kicad_sch"),
      subsheetPath: z.string().describe("Path for the new sub-sheet .kicad_sch to create"),
      sheetName: z.string().optional().default("Sheet").describe("Display name for the sheet"),
      position: z.object({ x: z.number(), y: z.number() }).optional(),
      size: z.object({ width: z.number(), height: z.number() }).optional(),
      metadata: z
        .record(z.string(), z.any())
        .optional()
        .describe("Optional metadata for the new sub-sheet (title, etc.)"),
    },
    async (args: any) => {
      const r = await callKicadScript("create_hierarchical_subsheet", args);
      return {
        content: [
          { type: "text", text: r.success ? r.message : `Failed: ${r.message || "Unknown error"}` },
        ],
      };
    },
  );

  // List every use of every sheet (instance paths and page numbers)
  server.tool(
    "list_sheet_instances",
    "List every sheet instance in the hierarchy below a root schematic, recursively: the root itself " +
      "and one entry per use of each sub-sheet (a sheet placed four times is listed four times). Each " +
      "entry has path (the KiCad instance path used in symbol (instances ...) entries: '/<root uuid>' " +
      "for the root, '/<root uuid>/<sheet block uuid>' below it), sheetName, file (as written in the " +
      "parent), filePath (absolute), page and parentPath (null for the root). Use the paths with " +
      "set_symbol_instance.",
    {
      schematicPath: z.string().describe("Path to the ROOT .kicad_sch of the project"),
    },
    async (args: { schematicPath: string }) => {
      const r = await callKicadScript("list_sheet_instances", args);
      if (!r.success)
        return {
          content: [{ type: "text", text: `Failed: ${r.message || "Unknown error"}` }],
          isError: true,
        };
      return {
        content: [{ type: "text", text: JSON.stringify(r.instances, null, 2) }],
      };
    },
  );

  // Set a symbol's reference/unit in one use of its sheet
  server.tool(
    "set_symbol_instance",
    "Set the reference and/or unit a placed symbol has in ONE use of its sheet, by editing (or " +
      'creating) its (path "<instancePath>" (reference ..) (unit ..)) entry. Lets one quad op-amp ' +
      "symbol on a sheet that is used four times be unit A of U501 in the first use, unit B in the " +
      "second, and so on: KiCad then netlists one U501 with the right pins per use. Identify the symbol " +
      "by uuid, or by reference (plus unit when several units of the part are placed in the file). " +
      "Get instancePath from list_sheet_instances. For the sheet's first use the Reference field and " +
      "the symbol's own unit follow, as KiCad writes them.",
    {
      schematicPath: z.string().describe("Path to the sheet .kicad_sch that contains the symbol"),
      uuid: z.string().optional().describe("uuid of the placed symbol"),
      reference: z
        .string()
        .optional()
        .describe("Reference of the symbol (its field or any instance reference), if no uuid"),
      unit: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe(
          "With reference: the unit the symbol has at instancePath, to pick one of several",
        ),
      instancePath: z
        .string()
        .describe("Instance path from list_sheet_instances, e.g. '/<root uuid>/<sheet uuid>'"),
      newReference: z.string().optional().describe("Reference in this use (e.g. U501)"),
      newUnit: z.number().int().min(1).optional().describe("Unit in this use (1=A, 2=B, ...)"),
    },
    async (args: {
      schematicPath: string;
      uuid?: string;
      reference?: string;
      unit?: number;
      instancePath: string;
      newReference?: string;
      newUnit?: number;
    }) => {
      const r = await callKicadScript("set_symbol_instance", args);
      if (!r.success)
        return {
          content: [{ type: "text", text: `Failed: ${r.message || "Unknown error"}` }],
          isError: true,
        };
      return { content: [{ type: "text", text: r.message }] };
    },
  );
}
