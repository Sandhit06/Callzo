# Callzo Usage Admin Console

React 18 and Tailwind CSS frontend for the Task 2 mock Usage API. The summary graphic uses accessible native SVG, so the project needs no chart dependency.

## Run

From this folder, run `docker compose up`. Compose starts the published frontend image and the mock API; open http://localhost:3000. The frontend container uses Nginx to proxy `/api/*` to `http://mock-api:8001/*`. Compose intentionally references an image only, as required by the assessment. Build and push the current source as `sandhit06/callzo-task2-frontend:latest` before submitting a changed version.

The supplied mock API is published for `linux/arm64`, so Compose requests that platform explicitly. Docker Desktop on x64 Windows uses ARM emulation to run it; the frontend image uses the host's default platform.

For local source development, run `npm install`, `npm run dev`, and open http://localhost:3000. Vite proxies API requests to `http://localhost:8001`.

## Behavior

- Server-side search, status/plan/country filters, sorting, page size and pagination are reflected in URL query parameters. The date range drives both the account list and summary request without navigation.
- Selections are a client-side set of immutable account IDs and survive filtering, paging, sorting, and background refreshes.
- Account rows refresh every 15 seconds. Requests are latest-wins; refresh errors keep the last successful rows visible and show a retry notice.
- The usage prism has independent loading, empty, error, stale-data and retry states. Its isometric bars aggregate longer date ranges into readable periods; minutes, SMS and spend can be switched in place, and bars support hover, keyboard focus, arrow-key movement and selection. The mock API's intermittent failure does not block the table.
- Settings load saved steps from the API. Fields save on blur and steps save before navigation. Server validation is shown on its returned step. The deliberate Step 4 then Step 2 rejection is explained inline; saved values and other drafts remain intact, and submitting can continue with the previously accepted Step 2 snapshot.
- The drawer supports Escape, focus return, a keyboard focus trap, labelled controls, and responsive layout down to 375px. Glass surfaces honor reduced motion, reduced transparency and higher contrast preferences.

## State choices

URL state holds filters, date range, sort and pagination so the view can be shared and refreshed. The API is the source of truth for account rows, summaries and saved settings. React state holds selection and in-progress settings drafts; selections use IDs so refetches do not clear them.

## Trade-offs

The table uses server pagination instead of downloading and virtualizing all 10,500 records. The settings drawer keeps a small local draft rather than adding a form library. The usage prism is custom SVG rather than a full 3D/WebGL engine: it gives the data dimensional facets and accessible interaction without introducing another dependency or making the console heavy.
