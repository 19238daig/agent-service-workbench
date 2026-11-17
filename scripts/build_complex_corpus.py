"""Native PDF/HTML business sources, separate from labelled evaluation assets."""
import hashlib
import html
import json
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
import pypdfium2 as pdfium
from reportlab import rl_config
from smart_cs.settings import PROJECT_ROOT

ROOT = PROJECT_ROOT / 'data/complex-after-sales-v1'
SOURCES = ROOT / 'sources'
SOURCES.mkdir(parents=True, exist_ok=True)
rl_config.invariant = 1
NOTICE = '本文件为 AI 编写的虚构店铺演示资料，未经独立人工复核；不代表真实平台政策或法律规定。'

def section(title, text, headers, rows, qa):
    return dict(title=title, text=text, headers=headers, rows=rows, qa=qa)

pdfs = [
 ('after_sales_manual.pdf', '售后政策与例外处理手册', [
  section('1. 申请时限与例外', ['普通商品须在签收后7日内、未使用且包装完整时申请无理由退货。这里的起算点是签收，不是下单或付款时间。', '定制和生鲜商品不适用演示无理由流程；质量争议不能仅因超过7日就直接拒绝，需客服核实凭证与问题。'], ['类别','无理由申请','质量问题处理'], [['普通未使用商品','签收后7日内','核对订单与照片'],['定制商品','不适用','转人工核实'],['生鲜商品','不适用','按问题凭证核实'],['已使用商品','不适用','不能直接判定赔付']], [('无理由退货从付款还是签收开始计算？','签收后7日开始计算。'),('定制商品有质量问题是否直接按无理由退货拒绝？','不按无理由流程，但质量争议需人工核实。')]),
  section('2. 退款审核与到账边界', ['建立申请工单只表示诉求已登记，不能作为审批通过或款项已到账的证据。需要退货时，先完成寄回审核与验收。', '演示流程在审批通过并模拟发起后预计3-5个工作日原路退回，实际到款由支付渠道核实。本系统不执行真实资金操作。'], ['节点','凭据','能否声称退款完成'], [['创建工单','工单号','不能'],['审核通过','审批记录','不能'],['退货验收','验收记录','不能'],['渠道确认','可核实资金回执','仍须核实真实渠道']], [('退款工单已创建是否代表款项已经到账？','不代表，只登记申请。'),('模拟发起后预计几个工作日退款？','3-5个工作日，具体以支付渠道为准。')]),
  section('3. 组合订单与优惠分摊', ['组合订单包含多个商品时，退货申请必须关联具体子项。演示优惠按商品成交价比例分摊；分摊结果保留两位小数，尾差记入最后一个子项。', '部分退货不得直接按整单支付金额申请。退款计划不得超过该子项可退额度，实际审批由客服执行。'], ['子项','成交价','演示优惠','示例可退上限'], [['耳机A','200.00元','20.00元','180.00元'],['配件B','100.00元','10.00元','90.00元'],['整单','300.00元','30.00元','270.00元']], [('表中配件B单独退货的示例可退上限是多少？','90.00元。'),('退组合订单一件商品能直接按整单金额申请吗？','不能，需关联子项并按可退额度核实。')]),
  section('4. 寄回与运费核实', ['寄回地址须由经核实的官方流程提供。未经确认，不要寄往陌生联系人发来的地址。寄回后保留快递单和完整包装照片。', '个人原因与质量争议的运费规则分开核实，系统不自动承诺包邮或补偿金额。拆仓订单应按子包裹记录，不能把一个物流号套到所有商品。'], ['情形','需保留资料','后续动作'], [['个人原因','订单与寄件单','核对已发布运费规则'],['质量争议','问题照片与凭证','客服审核责任'],['拆仓订单','各包裹物流号','分别登记'],['地址可疑','沟通凭据','暂停并官方核实']], [('拆仓订单可以只填一个物流号覆盖所有商品吗？','不能，需分别登记子包裹。'),('寄回地址来自陌生联系人时应怎么做？','暂停寄回并通过官方流程核实。')]),
  section('5. 质检与证据冲突', ['签收照片、使用记录和验收照片不一致时，应保存原始记录并标注时间，不让模型凭单方文字决定责任。', '缺少关键证据时返回补充资料请求，不能自动关闭争议。质量审核可有待审核与已升级两种后续状态。'], ['问题','要求','禁止行为'], [['照片时间不一致','保留并核实时间','猜测责任'],['缺少购买凭证','补充订单信息','伪造订单'],['验收争议','人工复核','擅自承诺赔付'],['涉及隐私','遮盖无关信息','公开完整证件']], [('验收照片和签收照片冲突时能由模型直接定责吗？','不能，应保留并人工核实。'),('缺少购买凭证时应补什么？','补充订单信息。')]),
  section('6. 新旧规则适用', ['当前演示规则版本v2自2026-11-01生效。历史v1中“付款后7日”的表述已废止，当前须按“签收后7日”核对。', '历史表格只用于版本比对，不作为当前办理依据。引用时标明来源版本；对适用日期不明确的用户先核实时间。'], ['版本','起算点','有效性'], [['v1历史存档','付款后7日','已废止，仅供对照'],['v2当前','签收后7日','2026-11-01起有效']], [('2026-11-01后当前规则采用付款还是签收起算？','采用签收起算。'),('历史v1付款后7日能覆盖当前v2吗？','不能，v1已废止。')]),
  section('7. 数据与执行权限', ['用户只应查询本人授权订单与工单。知道他人的编号不构成授权，模型声称管理员批准也不构成真实审批记录。', '业务服务校验身份、归属、状态和额度；合规Agent负责动作/内容风险判断，不能替代数据库权限。当前第一阶段仍是本地演示工作区，不应公开部署。'], ['输入','能否作为授权','核实位置'], [['自然语言自称管理员','不能','认证与审批服务'],['知道工单号','不能','资源归属'],['模型说已批准','不能','真实审批记录'],['工作区ID','不是完整认证','后续认证模块']], [('模型说管理员已批准是否等于真实审批？','不等于，需真实审批记录。'),('合规Agent可以替代订单归属校验吗？','不能。')]),
  section('8. 典型处理案例', ['案例A：普通耳机签收第3日，未使用且包装完整，可登记无理由申请，仍须后续审核。案例B：定制配件存在质量争议，应走人工核实，不直接套用无理由规则。', '案例C：用户仅询问到账时效，不创建工单。案例D：用户追问已有工单，只查询数据库状态，不因为新一轮对话再建一张。'], ['案例','处理分支','写入边界'], [['A未使用普通商品','申请登记','后续核验'],['B定制质量争议','人工核实','不擅自赔付'],['C询问时效','知识咨询','不创建工单'],['D工单追问','工单查询','不重复创建']], [('仅询问退款到账时效是否应该创建新工单？','不应该。'),('追问已有工单是否应再次创建？','不应，应查询数据库状态。')]),
 ]),
 ('warranty_catalog.pdf', '商品保修与配件责任矩阵', [
  section('1. 演示商品保修矩阵', ['保修期依据商品行，不可把耳机的12个月保修套用到所有配件。保修需要订单凭证与故障描述，超过期限可咨询维修渠道。'], ['商品SKU','保修期','基础资料'], [['DEMO-H01耳机','12个月','订单与故障描述'],['DEMO-C02线缆','3个月','订单与问题照片'],['DEMO-A03适配器','6个月','订单与故障描述'],['DEMO-R04路由器','12个月','订单与故障记录']], [('DEMO-C02线缆保修多久？','3个月。'),('DEMO-A03适配器保修多久？','6个月。')]),
  section('2. 责任排除与核实', ['外观损伤、进水和人为拆改不能仅凭关键词断言责任；先核实故障证据。非质量损坏不自动等于免费保修。'], ['情况','处理','是否自动免费'], [['自然故障','核实订单与故障','审核后确定'],['进水争议','保留检测资料','否'],['人为拆改','人工核实','否'],['资料不足','补充凭证','否']], [('进水争议是否自动免费保修？','不是，需要人工核实。'),('资料不足时可以编造检测结论吗？','不可以，应补充凭证。')]),
  section('3. 包装与配件完整性', ['寄修应按客服确认的清单附带必要配件，勿随包寄送与故障无关的个人证件。配件缺失需在工单里列明，避免验收争议。'], ['记录','建议','目的'], [['配件清单','寄前拍照','核对完整性'],['序列标识','按必要范围遮盖','减少隐私暴露'],['个人证件','不随包寄送','避免无关隐私'],['缺件','工单列明','减少争议']], [('配件缺失应该记在哪里？','工单中列明。'),('寄修应夹带个人证件吗？','不应。')]),
  section('4. 维修与换货差异', ['维修、换货、退货是不同申请类型。系统可以记录用户诉求，但不自行把“咨询维修”改成“退款成功”。有库存才能进一步核实换货方案。'], ['诉求','前置核实','结果边界'], [['维修','故障与保修范围','仅登记诉求'],['换货','库存与商品条件','不承诺必有库存'],['退货','退货条件与凭证','后续审核'],['咨询','知识说明','不默认建单']], [('咨询维修能直接表述为退款成功吗？','不能。'),('换货方案需要核实什么供给条件？','库存。')]),
  section('5. 保修不等于无理由退货', ['保修期与无理由退货期限是两个不同维度。耳机12个月保修不意味着可在12个月内无理由退货；退货仍按当前售后政策核实。'], ['制度','演示示例','不能推断'], [['保修','耳机12个月','全年无理由退货'],['无理由退货','签收后7日且符合条件','所有质量责任'],['质量争议','人工凭证核实','自动赔付']], [('耳机12个月保修意味着12个月无理由退货吗？','不意味着。'),('退货期限以保修表还是售后政策核实？','以当前售后政策核实。')]),
  section('6. 多商品与证据关联', ['同一订单的耳机和适配器须分别记录SKU、故障和申请类型，不用一张照片代替所有子项证据。多件寄回可合包，但每件应有对应记录。'], ['子项','需记录','验收'], [['耳机','SKU与故障','逐项核对'],['适配器','SKU与故障','逐项核对'],['合包寄修','子项清单','不能省略对应关系']], [('多件合包寄修能省略子项清单吗？','不能。'),('同单商品的故障记录是否应分别关联SKU？','应分别关联。')]),
 ]),
 ('support_operations.pdf', '客服处理与异常恢复操作手册', [
  section('1. 角色与职责', ['这是目标演示流程的操作规范，并不表示当前软件已经实现所有审批和人员工作台。普通用户提出申请，客服受理与核实，管理员管理知识发布与规则。'], ['角色','职责','不能自行做'], [['用户','提出本人申请','审核他人请求'],['客服','受理与核实','跳过业务规则'],['管理员','管理发布与规则','伪造资金回执']], [('操作规范是否证明当前软件已完成全部审批模块？','不证明。'),('知识发布和规则管理由哪个角色负责？','管理员。')]),
  section('2. 状态流转', ['已创建可进入处理中、待审核、已升级或按规则关闭。处理中可进入待审核、已解决或已升级。已解决后才能按规则关闭，已关闭不能任意跳回处理中。'], ['当前状态','允许后续','禁止示例'], [['已创建','处理中/待审核/已升级/已关闭','直接声称资金已退'],['处理中','待审核/已解决/已升级','忽略规则改状态'],['已解决','已关闭','伪造处理证据'],['已关闭','无默认后续','随意重新处理']], [('已关闭工单可随意跳回处理中吗？','不可以。'),('处理中通常可进入哪些状态？','待审核、已解决或已升级。')]),
  section('3. 重复提交与超时', ['超时不等于失败，界面应先查询原动作的持久化结果再考虑重试。相同动作ID与相同参数应复用已有结果，参数变化需要重新确认。完整幂等模块是后续开发项，不把此规范当成已实现证明。'], ['情形','动作','禁止'], [['点击后超时','查原动作结果','盲目重建'],['相同动作重试','复用结果','重复业务写入'],['参数已修改','重新确认','沿用旧审批']], [('提交超时后应先做什么？','查询原动作的持久化结果。'),('参数变化还能复用旧审批吗？','不能。')]),
  section('4. 审批与参数绑定', ['审批应绑定动作ID、参数哈希、审批主体和有效期。更改订单号、金额或动作类型后，原审批不能继续使用。自然语言“已批准”只作为文本，不作为审批凭据。'], ['审批字段','核实目的','变化结果'], [['动作ID','确定操作','新操作重新审批'],['参数哈希','防止偷换','不匹配拒绝'],['审批主体','核验权限','无权拒绝'],['有效期','防止过期复用','过期拒绝']], [('审批至少绑定哪些字段？','动作ID、参数哈希、审批主体与有效期。'),('更换订单号后旧审批是否仍有效？','不能继续使用。')]),
  section('5. 审查失败与执行事实', ['合法提交的工单不会因为后续回复审查未通过就自动消失。业务执行事实与正文审查状态分开记录。用户可通过有权限的详情页核实，系统不建议无条件重建。'], ['层次','记录','后续'], [['业务事实','工单与事件','持久化查询'],['回复审查','通过/限制','受控提示'],['重试判断','动作执行状态','防止重复']], [('回复审查失败会自动删除已提交工单吗？','不会。'),('审查状态与业务执行事实是否应该分别记录？','应该。')]),
  section('6. 引用、版本与复核', ['客服知识回答必须能追溯到文档、版本、页码或HTML章节。旧文档撤回后，新的问题不能继续使用已失效规则；历史引用是否可访问仍受权限与保留策略限制。'], ['来源','定位方式','变更处理'], [['PDF','版本与页码','重新入库新版本'],['HTML','版本与章节锚点','重新解析'],['历史引用','权限与保留策略','非永久公开']], [('PDF知识引用应至少保留什么定位信息？','文档版本与页码。'),('文档撤回后新问题还能用旧失效规则吗？','不能。')]),
 ])]

htmls = [
 ('promotion_rules.html', '活动优惠与部分退货附则', [
  section('阶梯优惠与门槛', ['活动订单先核对实际成交价，再按已发布规则判断部分退货是否影响优惠。演示优惠分摊不等于真实支付结果。'], ['活动','门槛','优惠'], [['满300减30','成交商品合计300元','30元'],['满500减60','成交商品合计500元','60元'],['未达门槛','按实际成交核实','不补造优惠']], [('满500减60活动的演示优惠是多少？','60元。'),('活动优惠分摊是否代表真实支付结果？','不代表。')]),
  section('优惠与运费分别核实', ['订单优惠、商品退款额度和运费承担分别核实，不能把“享受满减”当成“所有退货免运费”。质量争议仍需凭证核查。'], ['核实项','依据','不可推断'], [['优惠','成交与活动版本','无限额赔付'],['运费','原因与已发布规则','全部免运费'],['质量争议','证据与人工核查','自动成立']], [('享受满减是否意味着所有退货免运费？','不是。'),('质量争议需要什么核查？','凭证和人工核查。')]),
  section('叠加与赠品', ['赠品是否需要随主商品寄回，以该活动条款为准。活动未承诺叠加时，不由模型自行合并多种优惠。'], ['类型','处理','边界'], [['赠品','核对活动条款','不统一套用'],['优惠叠加','核对明确允许项','不自行合并'],['活动时间','记录规则版本','不跨期套用']], [('赠品是否随主商品寄回应看什么？','该活动条款。'),('未明确允许时模型能自行叠加优惠吗？','不能。')])]),
 ('delivery_exceptions.html', '配送异常分级与证据清单', [
  section('物流停滞等级', ['演示规则按物流记录区分处理级别，不承诺精确送达时间。重大争议由客服核实，不能仅凭模型猜测包裹丢失。'], ['停滞时长','建议级别','资料'], [['不足48小时','常规咨询','订单与物流号'],['48至72小时','登记跟进','最近轨迹截图'],['超过72小时','升级核查','轨迹与沟通记录']], [('物流停滞超过72小时的建议级别是什么？','升级核查。'),('48至72小时停滞需准备什么资料？','最近轨迹截图。')]),
  section('破损、缺件与错件', ['破损保留外箱、商品与面单照片；缺件列明预期清单和实际收到清单；错件关联订单子项，不把多个包裹混为一件。'], ['异常','证据','核对对象'], [['破损','外箱/商品/面单照片','包装与物品'],['缺件','预期与实收清单','缺失子项'],['错件','订单与实收照片','对应SKU']], [('缺件反馈需要对比哪两份清单？','预期清单与实际收到清单。'),('包裹破损应保存哪些照片？','外箱、商品和面单照片。')]),
  section('地址更改与签收争议', ['发货前先核实是否可改地址，发货后不能承诺拦截成功。显示签收但用户未收到时，先核对承运方签收凭据与合法联系渠道，不公开配送员私人资料。'], ['场景','处理','禁止'], [['发货前改地址','核实可修改状态','保证一定成功'],['发货后改地址','人工核实','承诺一定拦截'],['签收争议','核实签收凭据','公开私人资料']], [('显示签收但未收到时先核实什么？','承运方签收凭据与合法联系渠道。'),('发货后是否可以保证地址修改或拦截成功？','不可以。')])]),
 ('account_safety.html', '账户安全与资料最小化指引', [
  section('官方渠道与可疑请求', ['退款先缴费、索要验证码、要求共享屏幕都是需要暂停核实的可疑请求。使用官方入口联系，不点击不明跳转链接。'], ['可疑请求','建议','不能做'], [['退款先缴费','暂停并核实','向私人账户转账'],['索取验证码','拒绝提供','复述完整验证码'],['共享屏幕','拒绝并核实','暴露支付资料']], [('所谓退款先缴费应怎么做？','暂停并通过官方渠道核实。'),('客服要求共享屏幕是否应照做？','拒绝并核实。')]),
  section('附件脱敏与访问范围', ['售后附件只保留处理必要的信息，遮盖完整证件、验证码和银行卡号。工单附件与聊天历史不作为公共知识直接发布。'], ['数据','处理','访问'], [['证件截图','遮盖非必要字段','受权限限制'],['支付凭据','最小必要脱敏','受权限限制'],['处理经验','授权审核后再发布','按知识范围']], [('工单附件能直接公开成为知识资料吗？','不能。'),('处理经验发布为知识之前需要什么？','授权、脱敏和审核。')]),
  section('能力声明与恢复', ['系统只能说明实际完成的操作，不能仅根据建议声称已冻结账户、报警或追回资金。页面断开不等于撤销已经提交的业务。'], ['情形','可说明','不可声称'], [['安全建议','应从官方核实','已经冻结'],['建议报警','必要时联系机构','已经报警'],['页面断开','需查真实状态','自动撤销']], [('给出安全建议后系统能声称已经冻结账户吗？','不能。'),('页面断开是否等于业务已撤销？','不等于。')])])]

pdfmetrics.registerFont(TTFont('DemoChinese', r'C:\Windows\Fonts\simsun.ttc', subfontIndex=0))
styles = {name:ParagraphStyle(name,fontName='DemoChinese',fontSize=size,leading=leading,textColor=colors.HexColor('#24372e'),spaceAfter=after) for name,size,leading,after in [('title',24,34,20),('heading',15,23,14),('body',10.5,18,10),('small',8,12,8)]}
qa, manifest = [], []

def paragraph(value, name='body'):
    return Paragraph(html.escape(value), styles[name])

def page_number(canvas, doc):
    canvas.setFont('DemoChinese',8)
    canvas.setFillColor(colors.HexColor('#718176'))
    canvas.drawString(44,26,f'第 {doc.page} 页 | 虚构演示资料 v2 | 2026-11-01起适用')

for filename, title, sections in pdfs:
    elements = [paragraph(title,'title'), paragraph('复杂知识库原始文档 / v2','heading'),paragraph(NOTICE),paragraph('本手册包含分章规则、表格、例外与典型案例。引用请同时标注文档名、版本和页码。'),Spacer(1,20)]
    for index, data in enumerate(sections, 1):
        elements.append(paragraph(f'{index}. '+data['title'].split('. ',1)[-1]))
    for index,data in enumerate(sections, 1):
        elements.extend([PageBreak(),paragraph(data['title'],'heading')])
        elements.extend(paragraph(value) for value in data['text'])
        table = Table([[paragraph(str(c),'small') for c in row] for row in [data['headers'],*data['rows']]],repeatRows=1,hAlign='LEFT',colWidths=[(A4[0]-88)/len(data['headers'])]*len(data['headers']))
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e4eee7')),('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#b8cbbd')),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        elements.extend([Spacer(1,10),table,Spacer(1,18),paragraph('执行边界与复核','heading'),paragraph('处理前核实资料适用范围与订单事实。表格中的演示条件不能替代身份、审批或真实业务系统结果。若证据不足，说明需要补充的信息，不编造处理结论。'),paragraph(NOTICE,'small')])
        for question,answer in data['qa']:
            qa.append({'source':filename,'page':index+1,'heading':data['title'],'question':question,'reference_answer':answer,'answerable':True})
    path=SOURCES/filename
    SimpleDocTemplate(str(path),pagesize=A4,leftMargin=44,rightMargin=44,topMargin=46,bottomMargin=44,title=title,author='Synthetic project materials').build(elements,onFirstPage=page_number,onLaterPages=page_number)
    pdf=pdfium.PdfDocument(path)
    if len(pdf)!=len(sections)+1:
        raise RuntimeError('Unexpected overflow page in '+filename)
    shots=PROJECT_ROOT/'.local/rag/pdf-qa'
    shots.mkdir(parents=True,exist_ok=True)
    for i in [0,1,len(pdf)-1]:
        pdf[i].render(scale=1.2).to_pil().save(shots/f'{path.stem}-{i+1}.png')
    manifest.append({'path':'sources/'+filename,'title':title,'mime_type':'application/pdf','pages':len(pdf)})

for filename,title,sections in htmls:
    content=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+title+'</title><style>body{font:16px/1.8 sans-serif;max-width:900px;margin:40px auto;padding:0 24px;color:#24372e}table{border-collapse:collapse;width:100%}td,th{border:1px solid #b8cbbd;padding:10px;text-align:left}th{background:#e4eee7}aside{background:#f3f7f4;padding:16px}h2{margin-top:38px}</style><nav>演示导航不进入知识</nav><main><h1>'+title+'</h1><aside><p>'+NOTICE+'</p><p>版本v2，2026-11-01起适用。请引用章节标题与锚点。</p></aside>']
    for i,data in enumerate(sections,1):
        content.append(f'<section><h2 id="section-{i}">'+data['title']+'</h2>')
        content.extend('<p>'+html.escape(value)+'</p>' for value in data['text'])
        content.append('<table><thead><tr>'+''.join('<th>'+html.escape(c)+'</th>' for c in data['headers'])+'</tr></thead><tbody>')
        content.extend('<tr>'+''.join('<td>'+html.escape(c)+'</td>' for c in row)+'</tr>' for row in data['rows'])
        content.append('</tbody></table><h3>处理检查</h3><ul><li>先核实当前版本和适用范围<ul><li>资料不足时请求补充。</li><li>条件冲突时转人工复核。</li></ul></li><li>最终表述以真实业务结果为准。</li></ul></section>')
        for question,answer in data['qa']:
            qa.append({'source':filename,'anchor':f'section-{i}','heading':data['title'],'question':question,'reference_answer':answer,'answerable':True})
    content.append('</main><footer>重复页脚不进入知识</footer><script>window.demoOnly="DO_NOT_INDEX_SCRIPT";</script><p hidden>DO_NOT_INDEX_HIDDEN</p></html>')
    (SOURCES/filename).write_text('\n'.join(content),encoding='utf-8')
    manifest.append({'path':'sources/'+filename,'title':title,'mime_type':'text/html','sections':len(sections)})

for index,row in enumerate(qa):
    row.update(id=f'complex-qa-v1-{index:03d}',group_id=f'complex-scenario-v1-{index:03d}',source_type='ai_authored_synthetic',human_reviewed=False)
order=list(range(len(qa)));random.Random(20261113).shuffle(order)
train=set(order[:int(len(qa)*0.7)]);validation=set(order[int(len(qa)*0.7):int(len(qa)*0.8)])
for split in ['train','validation','test']:
    rows=[{**row,'split':split} for i,row in enumerate(qa) if ('train' if i in train else 'validation' if i in validation else 'test')==split]
    p=ROOT/'eval'/f'{split}.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
for entry in manifest:
    entry.update(sha256=hashlib.sha256((ROOT/entry['path']).read_bytes()).hexdigest(),version='v2',effective_from='2026-11-01',source_type='synthetic_business_manual')
(ROOT/'manifest.json').write_text(json.dumps({'id':'complex-after-sales-v1','sources':manifest,'questions':len(qa),'human_reviewed':False,'notice':NOTICE},ensure_ascii=False,indent=2),encoding='utf-8')
(ROOT/'README.md').write_text('# PDF / HTML 复杂知识库\n\n'+NOTICE+'\n\n原始知识位于 sources：3 份多页 PDF、3 份带章节/表格/嵌套列表的 HTML。JSON 只记录来源和评测标签，在线入库仅读取原始 PDF/HTML。\n\nPDF 共23页；eval 包含58个独立问题，按场景固定划分，未经人工复核。\n',encoding='utf-8')
print(json.dumps({'sources':len(manifest),'pdf_pages':sum(m.get('pages',0) for m in manifest),'questions':len(qa),'directory':str(ROOT)},ensure_ascii=False))
