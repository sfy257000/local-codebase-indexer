# Discovery and distribution

The project is positioned around a specific task: tracing SQL Server tables and procedures to C# callers, controller actions, and Razor pages. Search and other supported languages remain available; the bundled demo makes the primary workflow easy to verify.

## MCP Registry

The manifest name is `io.github.sfy257000/local-codebase-indexer`. The PyPI description contains the matching `mcp-name` ownership marker. The release workflow validates versions, publishes the package to PyPI, then uses GitHub OIDC to authenticate to the official MCP Registry. The official publisher is pinned to v1.8.8 and its Sigstore signature is verified before execution. No additional personal token is required by this workflow.

A valid local `server.json` is not proof of a live listing. Verify with:

```bash
curl --fail 'https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.sfy257000%2Flocal-codebase-indexer'
```

Check the returned name, version, and package arguments. The query returned no entry before the 0.8.0 publication work. If registry publication fails after PyPI succeeds, investigate the `mcp-registry` job and rerun failed jobs; do not republish an already-published PyPI version.

## GitHub discovery

The repository description and topics should match the C# / SQL Server workflow. Use real topics such as `sql-server`, `csharp`, `dotnet`, `mcp`, `code-search`, `static-analysis`, `sqlite`, and `tree-sitter`; remove misspellings and truncated topics. The case-study URL is a useful repository homepage.

GitHub stars, release asset downloads, PyPI downloads, and traffic measure different things. Release download counts exclude pip installations. The current GitHub integration cannot read this repository's traffic endpoints, so there is no verified view-to-install conversion rate.

## Submission and sharing materials

- [Directory copy and MCP configuration](launch/directory-entry.md)
- [English community post](launch/community.en.md)
- [Traditional Chinese community post](launch/community.zh-TW.md)
- [Source-backed case study](case-study.md)
- [Preview image](../assets/demo-preview.png) and [32-second GIF](../assets/demo.gif)

These drafts disclose authorship, clearly label the synthetic example, and avoid unmeasured savings claims. They are not evidence that a third-party directory accepted the project or a community post was sent. The owner selected drafts only for community posts and third-party directory submissions; these materials are retained without sending them. Check the destination's rules rather than sending the same text indiscriminately.
