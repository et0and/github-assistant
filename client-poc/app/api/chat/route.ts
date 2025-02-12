import { openai } from "@ai-sdk/openai";
import { generateObject, streamText, CoreMessage } from "ai";
import { z } from "zod";
import { ReltaApiClient } from "../../../lib/reltaApi";
import { AISDKExporter } from "langsmith/vercel";

export const maxDuration = 30;

const model = openai("gpt-4o");

const generateChartConfig = async (rows: object[]) => {
  const { object } = await generateObject({
    model,
    system:
      "You are a helpful assistant that answers questions about a GitHub repository. Identify the correct chart type based on the provided data.",
    prompt: JSON.stringify({
      rowCount: rows.length,
      rows: rows.length <= 6 ? rows : [...rows.slice(0, 3), ...rows.slice(-3)],
    }),
    schema: z.object({
      type: z.enum(["bar", "line", "pie"]),
      title: z.string(),
    }),
    experimental_telemetry: AISDKExporter.getSettings(),
  });
  return object;
};

const getRouterSystemPrompt = (
  repoName: string
) => `You are a helpful assistant that answers questions about the following GitHub repository:

Selected Repository: ${repoName}

You can use natural language queries to answer questions the user has about the repository.

If a question is best answered by displaying a graph/chart, use the "chart" tool.
If a question is about a single data point (e.g. "who made the most recent commit?"), use the "text" tool.

If using the "chart" tool, specify the x-Axis type and unit on the chart (e.g. "stars per day").

When printing a chart, ONLY call the provided function call. This will print the chart to the user. Do not use images.`;

export const POST = async (request: Request) => {
  const {
    owner,
    repo,
    messages: clientMessages,
  } = (await request.json()) as {
    owner: string;
    repo: string;
    messages: CoreMessage[];
  };

  const messages = clientMessages.map((m) => {
    if (m.role !== "tool") return m;
    return {
      ...m,
      content:
        typeof m.content === "string"
          ? m.content
          : m.content.map((c) => {
              if (c.toolName !== "chart") return c;

              const result = c.result as { rows: object[] };
              return {
                ...c,
                result: {
                  ...result,
                  rowCount: result.rows.length,
                  rows:
                    result.rows.length <= 6
                      ? result.rows
                      : [...result.rows.slice(0, 3), ...result.rows.slice(-3)],
                },
              };
            }),
    };
  });

  const relta = new ReltaApiClient({
    owner,
    repo_name: repo,
  });

  const stream = streamText({
    model,
    messages,
    system: getRouterSystemPrompt(`${owner}/${repo}`),
    tools: {
      chart: {
        description:
          "Query the GitHub metadata with the provided natural language query, and return the data as a table, which will be automatically displayed to the user in the form of a chart or table.",
        parameters: z.object({
          query: z.string().describe("The query to provide the agent."),
        }),
        execute: async (requestData) => {
          const { id, rows, sql } = await relta.getDataQuery(requestData.query);
          const config = await generateChartConfig(rows);
          return {
            ...config,
            id,
            rows,
            sql,
            hint:
              rows.length > 0
                ? "The chart is being displayed the user. As an LLM, you only see the first 3 rows and the last 3 rows."
                : "No data available.",
          };
        },
      },
      text: {
        description:
          "Query GitHub metadata with the provided natural language query, and return a natural language answer.",
        parameters: z.object({
          query: z.string().describe("The query to provide the agent."),
        }),
        execute: async (requestData) => {
          const { text, id } = await relta.getTextQuery(requestData.query);
          return { text, id };
        },
      },
    },
    experimental_telemetry: AISDKExporter.getSettings(),
  });

  return stream.toDataStreamResponse();
};
