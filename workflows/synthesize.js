export const meta = {
  name: 'sf-health-synthesize',
  description: 'sf-health: executive summary, area summaries and phased roadmap from accepted findings only',
  whenToUse: 'Called by /sf-health:assess after the consultant review gate. args: {run, pluginRoot}',
  phases: [
    { title: 'Synthesize', detail: 'one agent writes the summary and roadmap from accepted findings' },
  ],
}

const ITEM = {
  type: 'object', additionalProperties: false, required: ['title', 'finding_ids', 'effort', 'why'],
  properties: {
    title: { type: 'string', maxLength: 140 },
    finding_ids: { type: 'array', minItems: 1, items: { type: 'string' } },
    effort: { enum: ['S', 'M', 'L'] },
    why: { type: 'string', maxLength: 300 },
  },
}

const SYNTHESIS = {
  type: 'object', additionalProperties: false,
  required: ['kind', 'executive_summary', 'area_summaries', 'roadmap'],
  properties: {
    kind: { enum: ['sf-health-synthesis'] },
    executive_summary: {
      type: 'object', additionalProperties: false, required: ['headline', 'paragraphs', 'top_recommendations'],
      properties: {
        headline: { type: 'string', maxLength: 220 },
        paragraphs: { type: 'array', minItems: 2, maxItems: 4, items: { type: 'string', maxLength: 700 } },
        top_recommendations: {
          type: 'array', minItems: 3, maxItems: 3,
          items: {
            type: 'object', additionalProperties: false, required: ['text', 'finding_ids'],
            properties: { text: { type: 'string', maxLength: 300 }, finding_ids: { type: 'array', items: { type: 'string' } } },
          },
        },
      },
    },
    area_summaries: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false, required: ['area', 'summary'],
        properties: { area: { type: 'string' }, summary: { type: 'string', maxLength: 500 } },
      },
    },
    roadmap: {
      type: 'object', additionalProperties: false, required: ['quick_wins', 'short_term', 'long_term'],
      properties: {
        quick_wins: { type: 'array', items: ITEM },
        short_term: { type: 'array', items: ITEM },
        long_term: { type: 'array', items: ITEM },
      },
    },
  },
}

const run = args.run.replace(/\\/g, '/')
const pluginRoot = args.pluginRoot.replace(/\\/g, '/')

phase('Synthesize')
const result = await agent([
  `Read your instructions: ${pluginRoot}/agents/synthesizer.md (ignore its frontmatter) and follow them.`,
  `Input files (read all that exist):`,
  `- ${run}/findings/accepted.json`,
  `- ${run}/index/metrics.json`,
  `- ${run}/findings/baseline_diff.json`,
  `Use only Read. Set "kind" to "sf-health-synthesis".`,
].join('\n'), { label: 'synthesize', phase: 'Synthesize', schema: SYNTHESIS, ...(args.model ? { model: args.model } : {}) })

return result
  ? { ok: true, roadmapItems: ['quick_wins', 'short_term', 'long_term'].map(k => result.roadmap[k].length) }
  : { ok: false }
