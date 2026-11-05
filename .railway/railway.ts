import { defineRailway, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "bmschatgptbot";
export default defineRailway(() => {
  const bmschatgptbot = service("bmschatgptbot", {
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/healthz",
    healthcheckTimeout: 120,
    replicas: { "sfo": 1 },
  });

  return project("bmschatgptbot", {
    variables: { managed: false },
    resources: [bmschatgptbot],
  });
});
