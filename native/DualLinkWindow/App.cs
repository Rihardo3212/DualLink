using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Markup;
using Microsoft.UI.Xaml.XamlTypeInfo;

namespace DualLinkWindow;

/// <summary>
/// The application, written without XAML files: the standard control styles come from XamlControlsResources and
/// their type information from XamlControlsXamlMetaDataProvider.
/// </summary>
public partial class DualLinkApp : Application, IXamlMetadataProvider
{
    readonly XamlControlsXamlMetaDataProvider provider = new();
    readonly IEngine engine;
    MainView? view;

    public DualLinkApp(IEngine engine)
    {
        this.engine = engine;
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        Resources.MergedDictionaries.Add(new XamlControlsResources());
        view = new MainView(engine, Program.Opts);
        view.Activate();
    }

    public IXamlType GetXamlType(Type type) => provider.GetXamlType(type);
    public IXamlType GetXamlType(string fullName) => provider.GetXamlType(fullName);
    public XmlnsDefinition[] GetXmlnsDefinitions() => provider.GetXmlnsDefinitions();
}
