// [argus] Settings > Models hides upstream's provider presets: an admin (every
// Atlas owner) can add only the custom OpenAI-compatible endpoint there.
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  rs,
  test,
} from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ModelSettingsPage } from "@/components/workspace/settings/model-settings-page";
import { useAuth } from "@/core/auth/AuthProvider";
import { enUS } from "@/core/i18n/locales/en-US";
import type * as Management from "@/core/models/management";
import {
  loadManagedModelProviders,
  loadManagedModels,
  modelDraft,
  saveManagedModel,
  type ManagedModel,
  type ManagedModelProvider,
} from "@/core/models/management";
import {
  draftOnOfferedProvider,
  MODEL_PROVIDER_PRESETS_VISIBLE,
  offeredModelProviders,
} from "@/core/models/provider-presets";
import { isStaticWebsiteOnly } from "@/core/static-mode";

rs.mock("@/core/models/api", () => ({ loadModels: rs.fn() }));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: rs.fn() }));
rs.mock("@/core/auth/AuthProvider", () => ({ useAuth: rs.fn() }));
rs.mock("@/core/i18n/hooks", () => ({ useI18n: () => ({ t: enUS }) }));
rs.mock("@/core/models/management", () => ({
  ...rs.requireActual<typeof Management>("@/core/models/management"),
  loadManagedModels: rs.fn(),
  loadManagedModelProviders: rs.fn(),
  saveManagedModel: rs.fn(),
  testManagedModel: rs.fn(),
}));

const PROVIDERS: ManagedModelProvider[] = [
  {
    id: "openai",
    display_name: "OpenAI",
    requires_api_key: true,
    custom_endpoint: false,
  },
  {
    id: "anthropic",
    display_name: "Anthropic",
    requires_api_key: true,
    custom_endpoint: false,
  },
  {
    id: "openai-compatible",
    display_name: "Custom OpenAI-compatible",
    requires_api_key: false,
    custom_endpoint: true,
  },
];

const onPreset: ManagedModel = {
  ...modelDraft(),
  name: "direct",
  provider: "anthropic",
  model: "claude",
  display_name: "Direct",
  source: "managed",
  has_api_key: true,
  revision: "v1",
};

beforeEach(() => {
  rs.mocked(isStaticWebsiteOnly).mockReturnValue(false);
  rs.mocked(useAuth).mockReturnValue({
    user: { id: "admin-id", system_role: "admin" },
  } as ReturnType<typeof useAuth>);
  rs.mocked(loadManagedModelProviders).mockResolvedValue({
    providers: PROVIDERS,
  });
  rs.mocked(loadManagedModels).mockResolvedValue({ models: [] });
  rs.mocked(saveManagedModel).mockResolvedValue({
    ...onPreset,
    revision: "v2",
  });
});
afterEach(() => {
  cleanup();
  rs.clearAllMocks();
});

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ModelSettingsPage />
    </QueryClientProvider>,
  );
}

async function openProviderList() {
  fireEvent.pointerDown(screen.getByRole("combobox", { name: "Provider" }), {
    button: 0,
    ctrlKey: false,
    pointerType: "mouse",
  });
  return (await screen.findAllByRole("option")).map(
    (option) => option.textContent,
  );
}

describe("provider presets gate", () => {
  test("is closed on Argus", () => {
    expect(MODEL_PROVIDER_PRESETS_VISIBLE).toBe(false);
  });

  test("offers only the custom endpoint, plus an edited model's own provider", () => {
    expect(offeredModelProviders(PROVIDERS, false).map((p) => p.id)).toEqual([
      "openai-compatible",
    ]);
    expect(
      offeredModelProviders(PROVIDERS, false, "anthropic").map((p) => p.id),
    ).toEqual(["anthropic", "openai-compatible"]);
    expect(offeredModelProviders(PROVIDERS, true)).toEqual(PROVIDERS);
  });

  test("a new draft starts on the first offered provider", () => {
    const custom = offeredModelProviders(PROVIDERS, false);
    expect(draftOnOfferedProvider(modelDraft(), custom)).toMatchObject({
      provider: "openai-compatible",
      base_url: "",
    });
    expect(draftOnOfferedProvider(modelDraft(), PROVIDERS).provider).toBe(
      "openai",
    );
  });

  test("a new model can only use the custom endpoint", async () => {
    mount();
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Add model" })
          .hasAttribute("disabled"),
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add model" }));
    expect(screen.getByRole("combobox", { name: "Provider" }).textContent).toBe(
      enUS.settings.models.customProvider,
    );
    expect(await openProviderList()).toEqual([
      enUS.settings.models.customProvider,
    ]);
    fireEvent.keyDown(document.activeElement ?? document.body, {
      key: "Escape",
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "http://argus-litellm:4000/v1" },
    });
    fireEvent.change(screen.getByLabelText("Model ID"), {
      target: { value: "local-qwen" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saveManagedModel).toHaveBeenCalledTimes(1));
    expect(rs.mocked(saveManagedModel).mock.calls[0]![0].config).toMatchObject({
      provider: "openai-compatible",
      base_url: "http://argus-litellm:4000/v1",
      model: "local-qwen",
    });
  });

  test("a model saved on a preset stays editable on its provider", async () => {
    rs.mocked(loadManagedModels).mockResolvedValue({ models: [onPreset] });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Edit model" }));
    expect(screen.getByRole("combobox", { name: "Provider" }).textContent).toBe(
      "Anthropic",
    );
    expect(await openProviderList()).toEqual([
      "Anthropic",
      enUS.settings.models.customProvider,
    ]);
  });
});
