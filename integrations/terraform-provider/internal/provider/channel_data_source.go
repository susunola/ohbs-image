package provider

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type channelDataSource struct{ client *Client }
type channelModel struct {
	Bucket     types.String `tfsdk:"bucket"`
	Channel    types.String `tfsdk:"channel"`
	ImageID    types.String `tfsdk:"image_id"`
	Version    types.String `tfsdk:"version"`
	Region     types.String `tfsdk:"region"`
	Generation types.Int64  `tfsdk:"generation"`
}

func NewChannelDataSource() datasource.DataSource { return &channelDataSource{} }
func (d *channelDataSource) Metadata(_ context.Context, request datasource.MetadataRequest,
	response *datasource.MetadataResponse) {
	response.TypeName = request.ProviderTypeName + "_channel"
}
func (d *channelDataSource) Schema(_ context.Context, _ datasource.SchemaRequest,
	response *datasource.SchemaResponse) {
	response.Schema = schema.Schema{Attributes: map[string]schema.Attribute{
		"bucket": schema.StringAttribute{Required: true}, "channel": schema.StringAttribute{Required: true},
		"image_id": schema.StringAttribute{Computed: true}, "version": schema.StringAttribute{Computed: true},
		"region": schema.StringAttribute{Computed: true}, "generation": schema.Int64Attribute{Computed: true},
	}}
}
func (d *channelDataSource) Configure(_ context.Context, request datasource.ConfigureRequest,
	response *datasource.ConfigureResponse) {
	if request.ProviderData == nil {
		return
	}
	client, ok := request.ProviderData.(*Client)
	if !ok {
		response.Diagnostics.AddError("Invalid provider data", "Expected the ohbs-image client.")
		return
	}
	d.client = client
}
func (d *channelDataSource) Read(ctx context.Context, request datasource.ReadRequest,
	response *datasource.ReadResponse) {
	var state channelModel
	response.Diagnostics.Append(request.Config.Get(ctx, &state)...)
	if response.Diagnostics.HasError() {
		return
	}
	resolved, err := d.client.Resolve(ctx, state.Bucket.ValueString(), state.Channel.ValueString())
	if err != nil {
		response.Diagnostics.AddError("Unable to resolve channel", err.Error())
		return
	}
	state.ImageID = types.StringValue(resolved.Artifact.ID)
	state.Version = types.StringValue(resolved.Artifact.Version)
	state.Region = types.StringValue(resolved.Artifact.Region)
	state.Generation = types.Int64Value(resolved.Channel.Generation)
	response.Diagnostics.Append(response.State.Set(ctx, &state)...)
}
