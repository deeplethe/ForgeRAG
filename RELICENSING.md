# License change: MIT → AGPLv3 + commercial dual license

## Summary

OpenCraig (formerly ForgeRAG) is moving from the MIT License to the GNU
Affero General Public License v3.0 (AGPLv3), with a separate commercial
license available for organizations that need to use OpenCraig without
AGPLv3 obligations.

This change applies to all source releases tagged from this commit
forward. Releases tagged before the switch remain under the MIT License
they were originally published under — the MIT grant on those earlier
versions is **irrevocable** and continues to be valid for anyone who
received the code under MIT.

The original MIT license text is preserved at
[`LICENSE.MIT-historical`](LICENSE.MIT-historical) for reference.

## Why

OpenCraig is being developed as an Open Core product. The core remains
free for self-hosting (AGPLv3) and continues to evolve in public.
Organizations that:

* embed OpenCraig into proprietary / closed-source products, or
* deploy OpenCraig as a managed service competing with the upstream
  hosted offering,

would be subject to AGPLv3's "network use is distribution" and
copyleft clauses. They can purchase a commercial license to use
OpenCraig free of those obligations. Revenue from commercial licensing
funds full-time development on the AGPLv3 core, the same model used
by GitLab, MongoDB (pre-2018), Plausible, Element, Mattermost, and
Nextcloud.

## What changes for users

| You are… | What this means |
|---|---|
| **A solo user / individual researcher self-hosting OpenCraig** | Nothing changes in practice. You can use the AGPLv3 build for free, indefinitely, including for commercial work. |
| **A small team / lab self-hosting on internal infrastructure** | Same — internal use is fine under AGPLv3. You only need the commercial license if you redistribute the software or run it as a service for third parties. |
| **A company that wants to embed OpenCraig into a closed-source product** | You need a commercial license. AGPLv3 would otherwise require you to release the combined product's source. |
| **A cloud provider or SaaS company offering OpenCraig as a hosted product** | You need either to comply with AGPLv3 (publishing all source-side modifications) or to purchase a commercial license. |
| **Existing user of a pre-switch release** | You continue to hold MIT rights on that release. You can keep using, modifying, and redistributing under MIT. Upgrading to a post-switch release means accepting AGPLv3. |

## Contributor history

OpenCraig has accepted external contributions under the MIT License
prior to this switch. MIT is GPL-compatible — MIT-licensed contributions
can be incorporated into an AGPLv3-licensed combined work without
relicensing the individual contributions. The combined project is
AGPLv3 going forward; the original MIT-licensed code retains its MIT
notice at the file level for any downstream that wishes to extract
and use those files independently.

External contributors prior to the switch include:

* **Daniel Liang / Liangziyi** — `scripts/setup.py` auto-install feature
  (commits `aef2542`, `f217dc6`, `92081b2`)

We thank these contributors for their work, which remains MIT-licensed
at the file level and is incorporated into the AGPLv3 project under MIT
compatibility. If any past contributor would prefer their contribution
be removed or relicensed, please open a GitHub issue and we will
respond promptly.

## Future contributions

All contributions accepted from this commit forward must be made under
AGPLv3. Pull requests are reviewed against a Contributor License
Agreement (CLA) which grants the project the rights necessary to
distribute the contribution under AGPLv3 and to issue commercial
licenses derived from the codebase.

## Commercial licensing inquiries

Email: info@deeplethe.com

Please include a brief description of your intended deployment model
(internal use, embedded product, hosted service, etc.) so we can route
your inquiry efficiently.

## References

* [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)
* [Open Source vs. Open Core (Sentry)](https://blog.sentry.io/relicensing-sentry/)
* [License compatibility (FSF)](https://www.gnu.org/licenses/license-compatibility.html)
