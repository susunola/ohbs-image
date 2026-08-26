package provider

import (
	"context"
	"os"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type ohbsProvider struct{ version string }
type providerModel struct {
	Endpoint types.String `tfsdk:"endpoint"`
	Token    types.String `tfsdk:"token"`
}

func New(version string) func() provider.Provider {
	return func() provider.Provider { return &ohbsProvider{version: version} }
}

func (p *ohbsProvider) Metadata(_ context.Context, _ provider.MetadataRequest,
	response *provider.MetadataResponse) {
	response.TypeName = "ohbsimage"
	response.Version = p.version
}

func (p *ohbsProvider) Schema(_ context.Context, _ provider.SchemaRequest,
	response *provider.SchemaResponse) {
	response.Schema = schema.Schema{Attributes: map[string]schema.Attribute{
		"endpoint": schema.StringAttribute{Optional: true,
			Description: "ohbs-image control plane URL; defaults to OHBS_IMAGE_ENDPOINT."},
		"token": schema.StringAttribute{Optional: true, Sensitive: true,
			Description: "Bearer or OIDC token; defaults to OHBS_IMAGE_TOKEN."},
	}}
}

func (p *ohbsProvider) Configure(ctx context.Context, request provider.ConfigureRequest,
	response *provider.ConfigureResponse) {
	var config providerModel
	response.Diagnostics.Append(request.Config.Get(ctx, &config)...)
	if response.Diagnostics.HasError() {
		return
	}
	endpoint, token := config.Endpoint.ValueString(), config.Token.ValueString()
	if endpoint == "" {
		endpoint = os.Getenv("OHBS_IMAGE_ENDPOINT")
	}
	if token == "" {
		token = os.Getenv("OHBS_IMAGE_TOKEN")
	}
	if endpoint == "" || token == "" {
		response.Diagnostics.AddError("Missing ohbs-image configuration",
			"Set endpoint/token in the provider or OHBS_IMAGE_ENDPOINT/OHBS_IMAGE_TOKEN.")
		return
	}
	client := &Client{Endpoint: strings.TrimRight(endpoint, "/"), Token: token}
	response.DataSourceData = client
}

func (p *ohbsProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	return []func() datasource.DataSource{NewChannelDataSource}
}

func (p *ohbsProvider) Resources(_ context.Context) []func() resource.Resource { return nil }
